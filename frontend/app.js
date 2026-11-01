/* ============================================================
   API
============================================================ */

const API_BASE = "http://localhost:5678/webhook";

const DATA_URL = `${API_BASE}/555a4d0e-5bc8-4f27-83b8-6b5f06b68a0a-speed-test`;

const SUBMIT_URL = `${API_BASE}/5f046625-633b-43e9-9fd8-44b029a7b3a5-speed-test`;

const POSITIVE_REPORT_URL = `${API_BASE}/701829b4-73fd-4d10-aa1d-1a28c170051d-speed-test`;
const START_ANALYSIS_URL = `${API_BASE}/start-analysis-speed-test`;
const ANALYSIS_STATUS_URL = `${API_BASE}/doubao-analysis-status-v1`;
const ANALYSIS_PENDING_KEY = "doubao-pending-analysis-run-v1";
const ANALYSIS_POLL_INTERVAL_MS = 10000;
const ANALYSIS_DEADLINE_KEY = "doubao-analysis-wait-deadline-v1";

// 新分析批次可能先写入 PM 洞察，后生成 Positive Overall Report。
// 仅对本次新分析的批次等待报告，避免对缺少报告的历史批次无限轮询。
const POSITIVE_REPORT_WATCH_KEY = "doubao-positive-report-watch-v1";
const POSITIVE_REPORT_POLL_INTERVAL_MS = 10000;
const POSITIVE_REPORT_WAIT_MS = 30 * 60 * 1000;


// 批次列表以 n8n 的 pm_review_items 实际数据为准，不再把历史批次硬编码在前端。
const BATCH_SAVED_KEY = "doubao-custom-analysis-runs-v1";
const BATCH_SELECTED_KEY = "doubao-selected-analysis-run-v1";
const BATCH_ADD_OPTION = "__add_batch__";

let activeLoadController = null;
let activeLoadNumber = 0;
let analysisStartInProgress = false;
let analysisPollingRunId = null;
let analysisPollTimer = null;
let positiveReportPollTimer = null;
let positiveReportPollController = null;


/* ============================================================
   State
============================================================ */

const state = {
  items: [],

  problemFiltered: [],
  positiveFiltered: [],

  positiveReport: null,
  positiveReportEmptyMessage: "",

  currentPage: "dashboard",

  currentItem: null,

  selectedRunId: "",
  customBatches: [],

  loading: false,

  positiveReportLoading: false,
};

/* ============================================================
   DOM Helper
============================================================ */

const $ = (id) => document.getElementById(id);

async function readJsonResponse(response, contextLabel = "接口") {
  const body = await response.text();
  if (!body.trim()) {
    throw new Error(
      `${contextLabel}返回了空响应。请确认 n8n 已发布最新版主工作流，并开启空数据时仍输出结果的设置。`
    );
  }
  try {
    return JSON.parse(body);
  } catch {
    throw new Error(`${contextLabel}返回的内容不是有效 JSON，请检查 n8n Webhook 响应节点。`);
  }
}

/* ============================================================
   Helpers
============================================================ */

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function normalizeType(value) {
  return String(value || "")
    .trim()
    .toLowerCase() === "positive"
    ? "positive"
    : "problem";
}

function normalizeStatus(value) {
  const status = String(value || "")
    .trim()
    .toLowerCase();

  const allowed = [
    "pending",
    "approved",
    "modified",
    "rejected",
    "not_required",
  ];

  return allowed.includes(status) ? status : "pending";
}

function normalizePriority(value) {
  const priority = String(value || "")
    .trim()
    .toLowerCase();

  if (priority === "high" || priority === "medium" || priority === "low") {
    return priority;
  }

  return "excluded";
}

function normalizeItem(item) {
  return {
    ...item,

    insight_type: normalizeType(item.insight_type),

    review_status: normalizeStatus(item.review_status),

    priority_level: normalizePriority(item.priority_level),
  };
}

function splitValues(value) {
  return String(value || "")
    .split(/[\/,，;；|]/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function statusText(status) {
  const map = {
    pending: "待审核",
    approved: "已通过",
    modified: "已修改",
    rejected: "已驳回",
    not_required: "无需审核",
  };

  return map[normalizeStatus(status)] || "待审核";
}

function statusClass(status) {
  return "status-" + normalizeStatus(status);
}

function priorityClass(priority) {
  return "priority-" + normalizePriority(priority);
}

function displayPriority(item) {
  const priority = normalizePriority(item.priority_level);

  if (priority === "excluded") {
    return "Excluded";
  }

  const label = priority.charAt(0).toUpperCase() + priority.slice(1);

  if (
    item.priority_score !== null &&
    item.priority_score !== undefined &&
    item.priority_score !== ""
  ) {
    return label + " · " + item.priority_score;
  }

  return label;
}

function displayRank(item) {
  if (
    item.priority_rank === null ||
    item.priority_rank === undefined ||
    item.priority_rank === ""
  ) {
    return "—";
  }

  return "#" + item.priority_rank;
}

function formatDate(value) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return date.toLocaleString("zh-CN", {
    hour12: false,
  });
}

function showToast(message, type = "") {
  const toast = $("toast");

  toast.textContent = message;

  toast.className = `toast show ${type}`;

  clearTimeout(showToast.timer);

  showToast.timer = setTimeout(() => {
    toast.className = "toast";
  }, 2300);
}

/* ============================================================
   Navigation
============================================================ */

const pageConfig = {
  dashboard: {
    title: "Product Insight Dashboard",

    subtitle: "当前批次用户反馈的整体产品洞察",
  },

  problem: {
    title: "Problem Review",

    subtitle: "PM Human Review · 审核、修改和确认 AI Product Insight",
  },

  positive: {
    title: "Positive Insights",

    subtitle: "产品正向信号、核心优势与 Positive Cluster",
  },
};

function navigatePage(page, updateHash = true) {
  if (!pageConfig[page]) {
    page = "dashboard";
  }

  state.currentPage = page;

  document.querySelectorAll(".page").forEach((element) => {
    element.hidden = element.id !== `${page}Page`;
  });

  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });

  $("pageTitle").textContent = pageConfig[page].title;

  $("pageSubtitle").textContent = pageConfig[page].subtitle;

  $("reviewerWrap").hidden = page !== "problem";

  if (updateHash) {
    history.replaceState(null, "", `#${page}`);
  }

  closeDrawer();
}

/* ============================================================
   Analysis Batch Switcher
============================================================ */

function isValidBatchId(id) {
  return typeof id === "string" && /^run_[A-Za-z0-9_-]{4,100}$/.test(id);
}

function getBatchList() {
  return state.customBatches.map((id) => ({
    id,
    label: `分析批次 · ${id.replace(/^run_/, "").slice(0, 14)}`,
  }));
}

function renderBatchSelect() {
  const select = $("batchSelect");
  select.replaceChildren();

  const batches = getBatchList();
  if (batches.length === 0) {
    const emptyOption = new Option("暂无已入库批次", "");
    emptyOption.disabled = true;
    select.add(emptyOption);
  } else {
    batches.forEach(({ id, label }) => {
      select.add(new Option(label, id));
    });
  }

  select.add(new Option("＋ 添加其他批次…", BATCH_ADD_OPTION));
  select.value = state.selectedRunId || "";
}

function initBatchSelector() {
  // 不从 localStorage 恢复历史批次；先由服务器确认哪些批次仍有数据。
  state.customBatches = [];
  state.selectedRunId = "";
  renderBatchSelect();
}

async function refreshBatchListFromApi(preferredRunId = "") {
  const response = await fetch(DATA_URL, { cache: "no-store" });
  if (!response.ok) throw new Error(`PM Review API HTTP ${response.status}`);

  const result = await readJsonResponse(response, "PM Review API（批次列表）");
  if (result?.success !== true || !Array.isArray(result?.available_batches)) {
    throw new Error("PM Review API 未返回批次列表，请确认已导入并发布最新版主工作流。");
  }

  const ids = result.available_batches
    .map((batch) => typeof batch === "string" ? batch : batch?.id)
    .filter(isValidBatchId);

  state.customBatches = [...new Set(ids)].sort((a, b) => b.localeCompare(a));
  localStorage.setItem(BATCH_SAVED_KEY, JSON.stringify(state.customBatches));

  const savedId = localStorage.getItem(BATCH_SELECTED_KEY);
  state.selectedRunId =
    [preferredRunId, savedId, state.customBatches[0]]
      .find((id) => id && state.customBatches.includes(id)) || "";

  if (state.selectedRunId) {
    localStorage.setItem(BATCH_SELECTED_KEY, state.selectedRunId);
  } else {
    localStorage.removeItem(BATCH_SELECTED_KEY);
  }

  renderBatchSelect();
  return state.customBatches;
}

function resetBatchFilters() {
  ["problemSearch", "positiveSearch"].forEach((id) => {
    $(id).value = "";
  });
  [
    "problemStatusFilter", "problemPriorityFilter",
    "problemCategoryFilter", "problemFeatureFilter",
    "positiveCategoryFilter", "positiveFeatureFilter",
  ].forEach((id) => {
    $(id).value = "";
  });
}

function selectBatch(runId) {
  if (runId === state.selectedRunId) return;
  if (document.body.classList.contains("saving")) {
    $("batchSelect").value = state.selectedRunId;
    showToast("审核正在保存，请稍后切换批次", "error");
    return;
  }
  stopPositiveReportPolling();
  state.selectedRunId = runId;
  if (runId) {
    localStorage.setItem(BATCH_SELECTED_KEY, runId);
  } else {
    localStorage.removeItem(BATCH_SELECTED_KEY);
  }
  $("batchSelect").value = runId;
  $("batchAddForm").hidden = true;
  $("batchCount").textContent = "加载中…";
  closeDrawer();
  resetBatchFilters();
  loadEverything();
}

function showBatchAddForm() {
  $("batchSelect").value = state.selectedRunId;
  $("batchAddForm").hidden = false;
  $("batchIdInput").value = "";
  $("batchIdInput").focus();
}

async function handleBatchAdd(event) {
  event.preventDefault();
  const runId = $("batchIdInput").value.trim();
  if (!isValidBatchId(runId)) {
    showToast("请输入有效的 analysis_run_id，例如 run_20261011072726_463", "error");
    return;
  }
  if (getBatchList().some((item) => item.id === runId)) {
    selectBatch(runId);
    $("batchAddForm").hidden = true;
    return;
  }

  const button = $("batchAddBtn");
  button.disabled = true;
  try {
    // 先验证该批次已经入库，避免把不存在的编号加进列表。
    const response = await fetch(
      `${DATA_URL}?analysis_run_id=${encodeURIComponent(runId)}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`PM Review API HTTP ${response.status}`);
    const result = await readJsonResponse(response, "PM Review API");
    if (!Array.isArray(result?.data) || result.data.length === 0) {
      throw new Error("没有查到该批次的数据，请先确认已经在 n8n 入库");
    }
    if (result.analysis_run_id && result.analysis_run_id !== runId) {
      throw new Error("接口返回的批次编号不一致，请检查 n8n API");
    }
    if (result.data.some((row) => row.analysis_run_id !== runId)) {
      throw new Error("接口返回了其他批次的数据，请检查后端过滤规则");
    }

    state.customBatches.push(runId);
    localStorage.setItem(BATCH_SAVED_KEY, JSON.stringify(state.customBatches));
    renderBatchSelect();
    selectBatch(runId);
    showToast("已添加分析批次", "success");
  } catch (error) {
    showToast(error.message || "添加批次失败", "error");
  } finally {
    button.disabled = false;
  }
}

/* ============================================================
   Start analysis & discover a new batch (simplified Plan A)
   注意：检测到数据不等于证明整条 n8n 工作流已成功结束。
============================================================ */

// ============================================================
// 等待本次新批次的 Positive Overall Report（不修改 PM 洞察入库判定）
// ============================================================

function readPositiveReportWatch() {
  try {
    const watch = JSON.parse(localStorage.getItem(POSITIVE_REPORT_WATCH_KEY) || "null");
    if (
      watch &&
      isValidBatchId(watch.runId) &&
      Number.isFinite(watch.deadline) &&
      watch.deadline > 0
    ) return watch;
  } catch (error) {
    console.warn("正面报告等待记录无效：", error);
  }
  return null;
}

function createPositiveReportWatch(runId) {
  // 只有从「开始分析」产生的新批次才会进入报告等待。
  if (!isValidBatchId(runId)) return;
  const current = readPositiveReportWatch();
  if (current?.runId === runId) return;
  localStorage.setItem(POSITIVE_REPORT_WATCH_KEY, JSON.stringify({
    runId,
    deadline: Date.now() + POSITIVE_REPORT_WAIT_MS,
  }));
}

function stopPositiveReportPolling() {
  clearTimeout(positiveReportPollTimer);
  positiveReportPollTimer = null;
  if (positiveReportPollController) {
    positiveReportPollController.abort();
    positiveReportPollController = null;
  }
}

function positiveReportWaitMessage(runId = state.selectedRunId) {
  const hasPositiveInsights = state.items.some(
    item => item.insight_type === "positive"
  );

  // 已完成的批次如果没有正向洞察，显示后端保存的业务说明，而不是误报为报告生成中。
  if (!hasPositiveInsights) {
    return state.positiveReportEmptyMessage || "";
  }

  const watch = readPositiveReportWatch();
  if (
    !watch || watch.runId !== runId ||
    state.positiveReport
  ) return "";
  if (Date.now() >= watch.deadline) {
    return "正面洞察已入库，但正面报告未在等待时限内出现。后台可能仍在生成或已失败；请查看 n8n Executions，稍后可点击刷新。";
  }
  return "本批次正面洞察已入库，正面总结报告仍在生成；网页每 10 秒自动检查一次。";
}

function syncPositiveReportPolling(runId) {
  stopPositiveReportPolling();
  const watch = readPositiveReportWatch();
  if (!watch || watch.runId !== runId || state.selectedRunId !== runId) return;

  const hasPositiveInsights = state.items.some(
    item => item.insight_type === "positive"
  );
  if (!hasPositiveInsights || state.positiveReport) {
    localStorage.removeItem(POSITIVE_REPORT_WATCH_KEY);
    return;
  }
  schedulePositiveReportPoll(runId);
}

function schedulePositiveReportPoll(runId) {
  clearTimeout(positiveReportPollTimer);
  positiveReportPollTimer = null;
  const watch = readPositiveReportWatch();
  if (
    !watch || watch.runId !== runId ||
    state.selectedRunId !== runId ||
    state.positiveReport ||
    !state.items.some(item => item.insight_type === "positive")
  ) return;

  const remaining = watch.deadline - Date.now();
  if (remaining <= 0) {
    // 仅停止网页轮询，不把 n8n 后台任务判定为失败。
    renderPositiveReport();
    renderDashboardPositive();
    return;
  }

  positiveReportPollTimer = setTimeout(
    () => checkPositiveReportLater(runId),
    Math.min(POSITIVE_REPORT_POLL_INTERVAL_MS, remaining),
  );
}

async function checkPositiveReportLater(runId) {
  positiveReportPollTimer = null;
  const watch = readPositiveReportWatch();
  if (!watch || watch.runId !== runId || state.selectedRunId !== runId) return;

  const controller = new AbortController();
  positiveReportPollController = controller;
  try {
    // 只重新查询正面报告，不重载产品洞察和人工审核界面。
    const report = await loadPositiveReport(runId, controller.signal, true);
    if (
      controller.signal.aborted ||
      state.selectedRunId !== runId ||
      readPositiveReportWatch()?.runId !== runId
    ) return;

    if (report) {
      state.positiveReport = report;
      localStorage.removeItem(POSITIVE_REPORT_WATCH_KEY);
      renderPositiveReport();
      renderDashboard();
      showToast("正面反馈报告已生成并自动更新", "success");
      return;
    }
  } catch (error) {
    if (error.name !== "AbortError") console.warn("正面报告轮询失败：", error);
  } finally {
    if (positiveReportPollController === controller) {
      positiveReportPollController = null;
    }
  }
  if (state.selectedRunId === runId) schedulePositiveReportPoll(runId);
}

function showAnalysisStatus(message, tone = "muted") {
  const label = $("analysisStatus");
  label.textContent = message;
  label.dataset.tone = tone;
}

function updateAnalysisControls() {
  const isBusy = analysisStartInProgress || Boolean(analysisPollingRunId);
  $("startAnalysisBtn").disabled = isBusy;
  $("analysisLimit").disabled = isBusy;
  $("analysisSource").disabled = isBusy;
  $("analysisStopWaitBtn").hidden = !analysisPollingRunId;
}

function registerAnalysisBatch(runId) {
  if (getBatchList().some((batch) => batch.id === runId)) return;
  state.customBatches.push(runId);
  localStorage.setItem(BATCH_SAVED_KEY, JSON.stringify(state.customBatches));
  renderBatchSelect();
}

function scheduleAnalysisPoll(runId, delay = ANALYSIS_POLL_INTERVAL_MS) {
  clearTimeout(analysisPollTimer);
  if (analysisPollingRunId !== runId) return;

  const deadline = Number(localStorage.getItem(ANALYSIS_DEADLINE_KEY));

  if (Number.isFinite(deadline) && deadline > 0 && Date.now() >= deadline) {
    analysisPollingRunId = null;
    analysisPollTimer = null;
    localStorage.removeItem(ANALYSIS_PENDING_KEY);
    localStorage.removeItem(ANALYSIS_DEADLINE_KEY);
    updateAnalysisControls();
    showAnalysisStatus(
      `本次分析等待已超时。批次：${runId}。尚未确认整批数据入库完成。` +
      `后台可能仍在运行，也可能已经失败。请检查 n8n Executions。` +
      `这不会停止后台任务。`,
      "error",
    );
    return;
  }

  const remaining = deadline > 0 ? Math.max(0, deadline - Date.now()) : delay;
  analysisPollTimer = setTimeout(
    () => checkAnalysisResults(runId),
    Math.min(delay, remaining),
  );
}

function beginAnalysisPolling(runId, restored = false) {
  if (analysisPollingRunId && analysisPollingRunId !== runId) {
    clearTimeout(analysisPollTimer);
  }
  analysisPollingRunId = runId;
  localStorage.setItem(ANALYSIS_PENDING_KEY, runId);

  // 新任务设置等待截止时间；恢复任务时沿用此前的截止时间。
  if (!restored || !Number(localStorage.getItem(ANALYSIS_DEADLINE_KEY))) {
    const limit = Number($("analysisLimit").value);
    // 仅用于网页自动轮询的宽松上限，不是对 n8n 执行时长的预测。
    const waitMinutes = Math.max(
      30,
      Math.ceil(15 + (Number.isInteger(limit) ? limit : 1000) * 0.6),
    );
    localStorage.setItem(
      ANALYSIS_DEADLINE_KEY,
      String(Date.now() + waitMinutes * 60 * 1000),
    );
  }

  showAnalysisStatus(
    `${restored ? "已恢复等待" : "分析请求已提交"}：${runId}。正在等待本批次数据入库，每 10 秒检查一次。`,
    "running",
  );
  updateAnalysisControls();
  scheduleAnalysisPoll(runId, 1500);
}

// 新状态 API 是辅助判断；PM 洞察是否全部入库仍以 batch_write_complete 为准。
async function fetchAnalysisRunStatus(runId) {
  const response = await fetch(
    `${ANALYSIS_STATUS_URL}?analysis_run_id=${encodeURIComponent(runId)}`,
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(`分析状态接口 HTTP ${response.status}`);
  }

  const result = await readJsonResponse(response, "分析状态接口");
  if (!result || typeof result !== "object" || result.analysis_run_id !== runId) {
    throw new Error("分析状态接口返回的批次编号不一致");
  }
  if (result.success !== true) {
    throw new Error("分析状态接口返回无效请求或异常结果");
  }
  if (result.found === false && result.analysis_status === "not_found") {
    // 旧历史批次或任务状态尚未写入，不代表失败。
    return result;
  }
  const knownStates = ["running", "completed", "no_valid_reviews", "failed"];
  if (result.found !== true || !knownStates.includes(result.analysis_status)) {
    throw new Error("分析状态接口返回了无法识别的状态");
  }
  return result;
}

function finishAnalysisWaitingWithStatus(runId, message, tone) {
  if (analysisPollingRunId !== runId) return;
  clearTimeout(analysisPollTimer);
  analysisPollTimer = null;
  analysisPollingRunId = null;
  localStorage.removeItem(ANALYSIS_PENDING_KEY);
  localStorage.removeItem(ANALYSIS_DEADLINE_KEY);
  updateAnalysisControls();
  showAnalysisStatus(message, tone);
}

// 将常见的 n8n 节点名称转换成 PM 更容易理解的阶段名称。
function displayFailureStage(runStatus) {
  const rawStage = String(
    runStatus?.failed_node || runStatus?.analysis_stage || ""
  ).trim().replace(/^error:/i, "").trim();
  if (!rawStage) return "";

  const labels = {
    "DeepSeek - Quality Filter V1": "评论质量筛选",
    "Parse Quality Result": "评论质量筛选结果解析",
    "DeepSeek - Feedback Analyzer V2": "问题提取",
    "Parse Analyzer Result": "问题提取结果解析",
    "Parse Analyzer Result1": "问题提取结果解析",
    "DeepSeek - Issue Clustering": "问题聚类",
    "Parse Clustering Result": "聚类结果解析",
    "DeepSeek - Global Cluster Consolidation V1": "全局聚类汇总",
    "Parse Global Consolidation": "全局聚类结果解析",
    "DeepSeek - Problem Product Insight V1": "问题类产品洞察",
    "DeepSeek - Problem Product Insight Retry": "问题类产品洞察补充处理",
    "Parse - Problem Product Insight": "问题类产品洞察解析",
    "Parse - Problem Product Insight Retry": "问题类产品洞察补充结果解析",
    "DeepSeek - Positive Product Insight V1": "正向产品洞察",
    "Parse - Positive Product Insight": "正向产品洞察解析",
    "DeepSeek - Positive Overall Report": "整体正向报告生成",
    "Parse & Validate Positive Overall Report": "整体正向报告解析与校验",
    "Data Table - Upsert Positive Overall Report": "整体正向报告入库",
    "Data Table - Upsert Completed Status": "完成状态更新",
  };

  return labels[rawStage] || rawStage;
}

// 只有明确的终态才停止轮询；接口失败、not_found 和 completed 都不在此结束。
function handleTerminalAnalysisStatus(runId, runStatus) {
  if (runStatus?.analysis_status === "failed") {
    const failedStage = displayFailureStage(runStatus);
    const failureReason = String(
      runStatus.failure_reason || runStatus.status_message || ""
    ).trim().slice(0, 500);

    const details = [];
    if (failedStage) details.push(`失败阶段：${failedStage}`);
    if (failureReason) details.push(`原因：${failureReason}`);
    if (!details.length) {
      details.push("暂未获得可展示的失败原因，请查看 n8n 执行记录。 ");
    }

    finishAnalysisWaitingWithStatus(
      runId,
      `本次分析已失败（${runId}）。${details.join("；")}`,
      "error",
    );
    return true;
  }
  if (runStatus?.analysis_status === "no_valid_reviews") {
    const total = runStatus.total_review_count;
    const count = Number.isInteger(total) && total >= 0
      ? `，共检查 ${total} 条评论`
      : "";
    finishAnalysisWaitingWithStatus(
      runId,
      `本次分析已正常结束（${runId}）：没有可分析的有效评论${count}。无需等待 PM 洞察入库。`,
      "muted",
    );
    return true;
  }
  return false;
}

async function checkAnalysisResults(runId) {
  if (analysisPollingRunId !== runId) return;

  try {
    const response = await fetch(
      `${DATA_URL}?analysis_run_id=${encodeURIComponent(runId)}`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`查询接口 HTTP ${response.status}`);
    const result = await readJsonResponse(response, "PM Review API");
    if (result?.success !== true || !Array.isArray(result?.data)) {
      throw new Error("批次数据接口返回格式错误");
    }
    if (result.analysis_run_id !== runId ||
        result.data.some((item) => item.analysis_run_id !== runId)) {
      throw new Error("查询接口返回的批次与本次任务不一致");
    }
    if (analysisPollingRunId !== runId) return;

    if (result.data.length > 0 && result.batch_write_complete === true) {
      clearTimeout(analysisPollTimer);
      analysisPollingRunId = null;
      localStorage.removeItem(ANALYSIS_PENDING_KEY);
      localStorage.removeItem(ANALYSIS_DEADLINE_KEY);
      updateAnalysisControls();
      registerAnalysisBatch(runId);
      // PM 洞察入库完成不代表正面报告已生成，另开有时限的报告查询。
      createPositiveReportWatch(runId);
      if (document.body.classList.contains("saving")) {
        showAnalysisStatus(
          `本次批次 ${runId} 的 ${result.data.length} 条洞察已全部入库；当前正在保存人工审核，请稍后手动切换批次。`,
          "success",
        );
      } else {
        if (state.selectedRunId === runId) {
          loadEverything();
        } else {
          selectBatch(runId);
        }
        navigatePage("dashboard");
        showAnalysisStatus(
          `本次批次的 ${result.data.length} 条产品洞察已全部入库，并自动切换展示。`,
          "success",
        );
        showToast("已读取本次分析的新批次", "success");
      }
      return;
    }

    // 尚未确认 PM 洞察整批入库，向独立状态 API 查询当前批次。
    // 仅查询本次正在等待的批次，不改变历史批次、审核和正面报告加载流程。
    let runStatus = null;
    try {
      runStatus = await fetchAnalysisRunStatus(runId);
    } catch (statusError) {
      // 状态 API 暂时不可用时，维持原有轮询，不把网络问题误报为分析失败。
      console.warn("分析状态查询暂时失败：", statusError);
    }
    if (analysisPollingRunId !== runId) return;

    if (handleTerminalAnalysisStatus(runId, runStatus)) return;

    if (runStatus?.analysis_status === "completed") {
      // 保守处理：completed 表示主流程报告已写入状态表，
      // 但仍需要 batch_write_complete 才展示 PM 洞察。
      showAnalysisStatus(
        `主分析状态已完成（${runId}），但尚未从 PM 数据接口确认整批写入；10 秒后重试。`,
        "running",
      );
    } else if (runStatus?.analysis_status === "running") {
      const stage = String(runStatus.analysis_stage || "processing");
      showAnalysisStatus(
        `正在分析（${runId}，阶段：${stage}）：已查询到 ${result.data.length} 条洞察，10 秒后重新检查。`,
        "running",
      );
    } else {
      // 包括 not_found（兼容历史批次、启动后的短暂延迟）和查询暂时失败。
      showAnalysisStatus(
        `正在等待批次 ${runId}：已查询到 ${result.data.length} 条洞察，` +
        `尚未确认整批写入完成；状态未确定，10 秒后重试。`,
        "running",
      );
    }
  } catch (error) {
    if (analysisPollingRunId !== runId) return;
    console.error("查询 PM 洞察数据失败：", error);

    // 即使 PM 数据查询服务故障，仍尝试读取明确的分析终态。
    let runStatus = null;
    try {
      runStatus = await fetchAnalysisRunStatus(runId);
    } catch (statusError) {
      console.warn("分析状态查询暂时失败：", statusError);
    }
    if (analysisPollingRunId !== runId) return;
    if (handleTerminalAnalysisStatus(runId, runStatus)) return;

    showAnalysisStatus(
      `暂时无法查询批次 ${runId} 的 PM 洞察：${error.message}。` +
      `将自动重试；这不代表后台分析失败。`,
      "error",
    );
  }

  if (analysisPollingRunId === runId) scheduleAnalysisPoll(runId);
}

async function handleStartAnalysis(event) {
  event.preventDefault();
  if (analysisStartInProgress || analysisPollingRunId) return;

  const input = $("analysisLimit").value.trim();
  const limit = Number(input);
  if (!input || !Number.isInteger(limit) || limit < 10 || limit > 1000) {
    showAnalysisStatus("评论数量必须是 10～1000 之间的整数。", "error");
    return;
  }
  if (document.body.classList.contains("saving")) {
    showAnalysisStatus("审核正在保存，请稍后发起分析。", "error");
    return;
  }

  analysisStartInProgress = true;
  updateAnalysisControls();
  showAnalysisStatus(`正在提交 ${limit} 条评论的分析请求……`, "running");
  try {
    // 使用 CORS safelisted 的表单编码，避免 application/json 触发 OPTIONS 预检。
    // n8n Webhook 会将 application/x-www-form-urlencoded 解析为 input.body 对象。
    const requestBody = new URLSearchParams({
      source: $("analysisSource").value,
      limit: String(limit),
    });

    const response = await fetch(START_ANALYSIS_URL, {
      method: "POST",
      cache: "no-store",
      body: requestBody,
    });
    const payload = await readJsonResponse(response, "分析启动接口");
    if (!response.ok || payload?.success !== true) {
      throw new Error(payload?.message || `启动接口 HTTP ${response.status}`);
    }
    if (payload.status !== "request_received" ||
        !isValidBatchId(payload.analysis_run_id)) {
      throw new Error("启动接口没有返回有效的批次编号，请检查 n8n 控制工作流。");
    }
    beginAnalysisPolling(payload.analysis_run_id);
  } catch (error) {
    console.error("启动分析失败或响应无法确认：", error);
    const corsOrNetwork = error instanceof TypeError;
    showAnalysisStatus(
      corsOrNetwork
        ? "无法确认启动接口响应（可能是跨域或网络问题）。后台任务可能已经启动，请先检查 n8n Executions，不要立即再次提交。"
        : `分析请求未能确认：${error.message}。请检查控制工作流执行记录。`,
      "error",
    );
  } finally {
    analysisStartInProgress = false;
    updateAnalysisControls();
  }
}

function stopAnalysisWaiting() {
  const runId = analysisPollingRunId;
  if (!runId) return;
  analysisPollingRunId = null;
  clearTimeout(analysisPollTimer);
  localStorage.removeItem(ANALYSIS_PENDING_KEY);
  localStorage.removeItem(ANALYSIS_DEADLINE_KEY);
  updateAnalysisControls();
  showAnalysisStatus(
    `已停止网页自动等待。本次批次：${runId}；此操作不会取消后台分析，之后可通过「添加其他批次」查看结果。`,
    "muted",
  );
}

function restoreAnalysisWaiting() {
  const pendingRunId = localStorage.getItem(ANALYSIS_PENDING_KEY);
  if (!pendingRunId) return;
  if (!isValidBatchId(pendingRunId)) {
    localStorage.removeItem(ANALYSIS_PENDING_KEY);
    localStorage.removeItem(ANALYSIS_DEADLINE_KEY);
    return;
  }
  beginAnalysisPolling(pendingRunId, true);
}

/* ============================================================
   Load Data
============================================================ */

async function loadEverything() {
  stopPositiveReportPolling();
  // 连续切换批次时取消旧请求，避免旧批次数据晚返回后覆盖新数据。
  if (activeLoadController) activeLoadController.abort();
  const runId = state.selectedRunId;

  if (!runId) {
    ++activeLoadNumber;
    activeLoadController = null;
    state.loading = false;
    state.items = [];
    state.positiveReport = null;
    state.positiveReportEmptyMessage = "";
    sortAllItems();
    renderEverything();
    $("globalLoading").hidden = true;
    $("globalError").hidden = true;
    $("batchCount").textContent = "暂无批次数据";
    $("refreshBtn").disabled = false;
    return;
  }

  const controller = new AbortController();
  activeLoadController = controller;
  const thisLoad = ++activeLoadNumber;

  state.loading = true;
  state.positiveReport = null;
  state.positiveReportEmptyMessage = "";
  $("globalLoading").hidden = false;
  $("globalError").hidden = true;
  $("batchCount").textContent = "加载中…";
  $("refreshBtn").disabled = true;

  try {
    const [items, report] = await Promise.all([
      loadReviewData(runId, controller.signal),
      loadPositiveReport(runId, controller.signal),
    ]);
    if (thisLoad !== activeLoadNumber) return;

    state.items = items;
    state.positiveReport = report;

    // 没有正向洞察且没有报告时，查询批次状态；若后端已正常完成且说明没有正向反馈，
    // 在报告空状态区域展示业务说明，避免 PM 将“无报告”误认为程序故障。
    if (
      !report &&
      !items.some(item => item.insight_type === "positive")
    ) {
      state.positiveReportEmptyMessage =
        await loadPositiveReportEmptyMessage(runId, controller.signal);
      if (thisLoad !== activeLoadNumber) return;
    }

    sortAllItems();
    renderEverything();
    syncPositiveReportPolling(runId);
    $("batchCount").textContent = `${state.items.length} 条洞察`;
    $("globalLoading").hidden = true;
  } catch (error) {
    if (error.name === "AbortError" || thisLoad !== activeLoadNumber) return;
    console.error(error);
    state.items = [];
    state.positiveReport = null;
    state.positiveReportEmptyMessage = "";
    renderEverything();
    $("batchCount").textContent = "加载失败";
    $("globalLoading").hidden = true;
    $("globalError").hidden = false;
    $("globalErrorText").textContent = error.message || "未知错误";
  } finally {
    if (thisLoad === activeLoadNumber) {
      state.loading = false;
      activeLoadController = null;
      $("refreshBtn").disabled = false;
    }
  }
}

async function loadReviewData(runId, signal) {
  const response = await fetch(
    `${DATA_URL}?analysis_run_id=${encodeURIComponent(runId)}`,
    { cache: "no-store", signal },
  );
  if (!response.ok) throw new Error(`PM Review API HTTP ${response.status}`);

  const result = await readJsonResponse(response, "PM Review API");
  if (!result || !Array.isArray(result.data)) {
    throw new Error("PM Review API 返回格式不正确");
  }
  if (result.analysis_run_id && result.analysis_run_id !== runId) {
    throw new Error("PM Review API 返回了错误的分析批次");
  }
  if (result.data.some((item) => item.analysis_run_id !== runId)) {
    throw new Error("接口中混入其他分析批次，请检查 n8n 过滤规则");
  }
  return result.data.map(normalizeItem);
}

async function loadPositiveReportEmptyMessage(runId, signal) {
  try {
    const response = await fetch(
      `${ANALYSIS_STATUS_URL}?analysis_run_id=${encodeURIComponent(runId)}`,
      { cache: "no-store", signal },
    );
    if (!response.ok) return "";

    const result = await readJsonResponse(response, "分析状态接口");
    const message = String(result?.status_message || "").trim();

    if (
      result?.success === true &&
      result?.found === true &&
      result?.analysis_run_id === runId &&
      result?.analysis_status === "completed" &&
      /没有正向反馈|未发现可汇总的正向反馈/.test(message)
    ) {
      return message.slice(0, 300);
    }
  } catch (error) {
    if (error.name === "AbortError") throw error;
    console.warn("读取正向报告空状态说明失败：", error);
  }

  return "";
}

async function loadPositiveReport(runId, signal, quiet = false) {
  state.positiveReportLoading = true;
  try {
    const response = await fetch(
      `${POSITIVE_REPORT_URL}?analysis_run_id=${encodeURIComponent(runId)}`,
      { cache: "no-store", signal },
    );
    if (!response.ok) throw new Error(`Positive Report HTTP ${response.status}`);

    const result = await readJsonResponse(response, "Positive Report API");
    const report = result?.success ? result.data : null;
    $("positiveReportError").hidden = true;

    if (!report) return null;
    // 后端已按 analysis_run_id 筛选；前端仍执行二次校验。
    // 兼容没有 analysis_run_id 的旧报告：只允许显示在原历史演示批次。
    if (report.analysis_run_id) {
      return report.analysis_run_id === runId ? report : null;
    }
    return null;
  } catch (error) {
    if (error.name === "AbortError") throw error;
    if (!quiet) {
      console.error(error);
      $("positiveReportError").hidden = false;
      $("positiveReportErrorText").textContent = error.message;
    } else {
      console.warn("正面报告暂时无法查询，将按时限重试：", error);
    }
    return null;
  } finally {
    state.positiveReportLoading = false;
  }
}

function sortAllItems() {
  state.items.sort((a, b) => {
    if (a.insight_type === "problem" && b.insight_type === "positive") {
      return -1;
    }

    if (a.insight_type === "positive" && b.insight_type === "problem") {
      return 1;
    }

    const rankA = Number(a.priority_rank);

    const rankB = Number(b.priority_rank);

    const validA = Number.isFinite(rankA) && rankA > 0;

    const validB = Number.isFinite(rankB) && rankB > 0;

    if (validA && validB) {
      return rankA - rankB;
    }

    if (validA) {
      return -1;
    }

    if (validB) {
      return 1;
    }

    return Number(b.feedback_count || 0) - Number(a.feedback_count || 0);
  });
}

/* ============================================================
   Render Everything
============================================================ */

function renderEverything() {
  populateFilters();

  renderProblemStats();

  applyProblemFilters();

  applyPositiveFilters();

  renderPositiveReport();

  renderDashboard();
}

/* ============================================================
   Dashboard
============================================================ */

function getProblemItems() {
  return state.items.filter((item) => item.insight_type === "problem");
}

function getPositiveItems() {
  return state.items.filter((item) => item.insight_type === "positive");
}

function sumFeedback(items) {
  return items.reduce((sum, item) => sum + Number(item.feedback_count || 0), 0);
}

function renderDashboard() {
  const problems = getProblemItems();

  const positives = getPositiveItems();

  const problemFeedback = sumFeedback(problems);

  const positiveFeedback =
    state.positiveReport?.positive_feedback_count ?? sumFeedback(positives);

  const pending = problems.filter(
    (item) => item.review_status === "pending",
  ).length;

  const approved = problems.filter(
    (item) => item.review_status === "approved",
  ).length;

  const modified = problems.filter(
    (item) => item.review_status === "modified",
  ).length;

  const rejected = problems.filter(
    (item) => item.review_status === "rejected",
  ).length;

  const reviewed = approved + modified + rejected;

  const reviewPercent = problems.length
    ? (reviewed / problems.length) * 100
    : 0;

  const highPriority = problems.filter(
    (item) => item.priority_level === "high",
  ).length;

  $("dashboardKpis").innerHTML =
    createKpi(
      "Problem Clusters",
      problems.length,
      "待 PM 判断的问题洞察",
      "primary",
    ) +
    createKpi(
      "Positive Clusters",
      positives.length,
      "无需 Human Review",
      "green",
    ) +
    createKpi(
      "Problem Feedback",
      problemFeedback,
      "Problem Cluster 反馈量汇总",
    ) +
    createKpi(
      "Positive Feedback",
      positiveFeedback,
      "当前批次正向反馈量",
      "green",
    ) +
    createKpi(
      "Review Progress",
      `${reviewPercent.toFixed(1)}%`,
      `${reviewed} / ${problems.length} reviewed`,
      "primary",
    ) +
    createKpi("High Priority", highPriority, "当前高优问题数", "red");

  $("dashboardReviewPercent").textContent = `${reviewPercent.toFixed(1)}%`;

  $("dashboardReviewBar").style.width = `${reviewPercent}%`;

  $("dashboardReviewStats").innerHTML =
    createReviewStatusCard("Pending", pending) +
    createReviewStatusCard("Approved", approved) +
    createReviewStatusCard("Modified", modified) +
    createReviewStatusCard("Rejected", rejected);

  renderDashboardTopProblems(problems);

  renderDistribution(problems, "issue_categories", "categoryDistribution");

  renderDistribution(problems, "issue_features", "featureDistribution");

  renderDashboardPositive();
}

function createKpi(label, value, helper, className = "") {
  return `
    <article class="kpi-card ${className}">

      <div class="kpi-label">
        ${escapeHtml(label)}
      </div>

      <div class="kpi-value">
        ${escapeHtml(value)}
      </div>

      <div class="kpi-helper">
        ${escapeHtml(helper)}
      </div>

    </article>
  `;
}

function createReviewStatusCard(label, value) {
  return `
    <div class="review-status-card">

      <span>
        ${escapeHtml(label)}
      </span>

      <strong>
        ${escapeHtml(value)}
      </strong>

    </div>
  `;
}

function getFinalInsight(item) {
  if (item.review_status === "rejected") {
    return {
      rejected: true,
      text: "该 Insight 已被 PM 驳回，不作为最终产品结论。",
    };
  }

  if (
    item.review_status === "modified" &&
    String(item.pm_insight_summary || "").trim()
  ) {
    return {
      rejected: false,
      text: item.pm_insight_summary,
    };
  }

  return {
    rejected: false,
    text: item.insight_summary || "—",
  };
}

function renderDashboardTopProblems(problems) {
  const top = [...problems]
    .sort((a, b) => {
      const rankA = Number(a.priority_rank) || 999999;

      const rankB = Number(b.priority_rank) || 999999;

      return rankA - rankB;
    })
    .slice(0, 10);

  $("dashboardTopProblems").innerHTML = top
    .map((item) => {
      const finalInsight = getFinalInsight(item);

      const insightHtml = finalInsight.rejected
        ? `
                <div class="rejected-insight">
                  ${escapeHtml(finalInsight.text)}
                </div>
              `
        : `
                <div class="final-insight">
                  ${escapeHtml(finalInsight.text)}
                </div>
              `;

      return `
            <tr>

              <td>
                ${escapeHtml(displayRank(item))}
              </td>

              <td>

                <div class="cluster-name">
                  ${escapeHtml(item.final_cluster_name || "—")}
                </div>

                <div class="cluster-id">
                  ${escapeHtml(item.final_cluster_id || "")}
                </div>

              </td>

              <td>
                ${escapeHtml(item.feedback_count || 0)}
              </td>

              <td>

                <span class="badge ${priorityClass(item.priority_level)}">
                  ${escapeHtml(displayPriority(item))}
                </span>

              </td>

              <td>

                <span class="badge ${statusClass(item.review_status)}">
                  ${escapeHtml(statusText(item.review_status))}
                </span>

              </td>

              <td>
                ${insightHtml}
              </td>

              <td>

                <button
                  class="detail-button dashboard-problem-detail"
                  data-review-key="${escapeHtml(item.review_key)}"
                >
                  查看
                </button>

              </td>

            </tr>
          `;
    })
    .join("");
}

function renderDistribution(items, field, containerId) {
  const map = new Map();

  items.forEach((item) => {
    const values = splitValues(item[field]);

    const weight = Number(item.feedback_count || 0);

    values.forEach((value) => {
      map.set(value, (map.get(value) || 0) + weight);
    });
  });

  const rows = [...map.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);

  const max = rows.length ? rows[0][1] : 1;

  const container = $(containerId);

  if (rows.length === 0) {
    container.innerHTML = '<div class="cell-muted">暂无数据</div>';

    return;
  }

  container.innerHTML = rows
    .map(([label, value]) => {
      const width = (value / max) * 100;

      return `
            <div class="bar-item">

              <div
                class="bar-label"
                title="${escapeHtml(label)}"
              >
                ${escapeHtml(label)}
              </div>

              <div class="bar-track">

                <div
                  class="bar-fill"
                  style="width:${width}%"
                ></div>

              </div>

              <div class="bar-value">
                ${escapeHtml(value)}
              </div>

            </div>
          `;
    })
    .join("");
}

function renderDashboardPositive() {
  const container = $("dashboardPositiveHighlights");

  const report = state.positiveReport;

  if (!report) {
    const waiting = positiveReportWaitMessage();
    container.innerHTML = `
      <div class="cell-muted">
        ${escapeHtml(waiting || "暂无 Positive Overall Report")}
      </div>
    `;

    return;
  }

  const strengths = Array.isArray(report.core_strengths)
    ? report.core_strengths
    : [];

  container.innerHTML = `

    <div class="dashboard-positive-meta">

      ${escapeHtml(report.positive_cluster_count ?? 0)}
      Positive Clusters ·

      ${escapeHtml(report.positive_feedback_count ?? 0)}
      Feedback

    </div>


    <div class="dashboard-positive-summary">

      ${escapeHtml(report.overall_summary || "—")}

    </div>


    <div class="dashboard-strengths">

      ${strengths
        .slice(0, 5)
        .map(
          (strength) => `
            <span class="dashboard-strength-chip">
              ${escapeHtml(strength.strength_name || "")}
            </span>
          `,
        )
        .join("")}

    </div>
  `;
}

/* ============================================================
   Problem Stats
============================================================ */

function renderProblemStats() {
  const problems = getProblemItems();

  const pending = problems.filter(
    (item) => item.review_status === "pending",
  ).length;

  const approved = problems.filter(
    (item) => item.review_status === "approved",
  ).length;

  const modified = problems.filter(
    (item) => item.review_status === "modified",
  ).length;

  const rejected = problems.filter(
    (item) => item.review_status === "rejected",
  ).length;

  const reviewed = approved + modified + rejected;

  const percent = problems.length ? (reviewed / problems.length) * 100 : 0;

  $("problemStatsGrid").innerHTML =
    createStat("Problem 总数", problems.length) +
    createStat("待审核", pending, "pending") +
    createStat("已通过", approved, "approved") +
    createStat("已修改", modified, "modified") +
    createStat("已驳回", rejected, "rejected");

  $("problemProgressText").textContent = `${reviewed} / ${problems.length}`;

  $("problemProgressPercent").textContent = `${percent.toFixed(1)}%`;

  $("problemProgressBar").style.width = `${percent}%`;
}

function createStat(label, value, className = "") {
  return `
    <div class="stat-card ${className}">

      <div class="stat-label">
        ${escapeHtml(label)}
      </div>

      <div class="stat-value">
        ${escapeHtml(value)}
      </div>

    </div>
  `;
}

/* ============================================================
   Filters
============================================================ */

function populateFilters() {
  populateFilterForType(
    "problem",
    $("problemCategoryFilter"),
    $("problemFeatureFilter"),
  );

  populateFilterForType(
    "positive",
    $("positiveCategoryFilter"),
    $("positiveFeatureFilter"),
  );
}

function populateFilterForType(type, categoryElement, featureElement) {
  const rows = state.items.filter((item) => item.insight_type === type);

  const categories = new Set();

  const features = new Set();

  rows.forEach((item) => {
    splitValues(item.issue_categories).forEach((value) =>
      categories.add(value),
    );

    splitValues(item.issue_features).forEach((value) => features.add(value));
  });

  categoryElement.innerHTML = buildOptions(
    "全部 Category",
    [...categories].sort(),
  );

  featureElement.innerHTML = buildOptions("全部 Feature", [...features].sort());
}

function buildOptions(firstLabel, values) {
  return (
    `<option value="">
      ${escapeHtml(firstLabel)}
    </option>` +
    values
      .map(
        (value) => `
          <option value="${escapeHtml(value)}">
            ${escapeHtml(value)}
          </option>
        `,
      )
      .join("")
  );
}

/* ============================================================
   Problem Table
============================================================ */

function applyProblemFilters() {
  const keyword = $("problemSearch").value.trim().toLowerCase();

  const status = $("problemStatusFilter").value;

  const priority = $("problemPriorityFilter").value;

  const category = $("problemCategoryFilter").value;

  const feature = $("problemFeatureFilter").value;

  state.problemFiltered = getProblemItems().filter((item) => {
    if (keyword) {
      const haystack =
        `${item.final_cluster_name || ""} ${item.final_cluster_id || ""}`.toLowerCase();

      if (!haystack.includes(keyword)) {
        return false;
      }
    }

    if (status && item.review_status !== status) {
      return false;
    }

    if (priority && item.priority_level !== priority) {
      return false;
    }

    if (category && !String(item.issue_categories || "").includes(category)) {
      return false;
    }

    if (feature && !String(item.issue_features || "").includes(feature)) {
      return false;
    }

    return true;
  });

  renderProblemTable();
}

function renderProblemTable() {
  $("problemResultCount").textContent = `${state.problemFiltered.length} 条`;

  $("problemEmpty").hidden = state.problemFiltered.length > 0;

  $("problemTableBody").innerHTML = state.problemFiltered
    .map(
      (item) => `
          <tr>

            <td>
              ${escapeHtml(displayRank(item))}
            </td>

            <td>

              <div class="cluster-name">
                ${escapeHtml(item.final_cluster_name || "—")}
              </div>

              <div class="cluster-id">
                ${escapeHtml(item.final_cluster_id || "")}
              </div>

            </td>

            <td class="cell-muted">
              ${escapeHtml(item.issue_categories || "—")}
            </td>

            <td class="cell-muted">
              ${escapeHtml(item.issue_features || "—")}
            </td>

            <td>
              ${escapeHtml(item.feedback_count || 0)}
            </td>

            <td>

              <span class="badge ${priorityClass(item.priority_level)}">
                ${escapeHtml(displayPriority(item))}
              </span>

            </td>

            <td>

              <span class="badge ${statusClass(item.review_status)}">
                ${escapeHtml(statusText(item.review_status))}
              </span>

            </td>

            <td>

              <button
                class="detail-button insight-detail-button"
                data-review-key="${escapeHtml(item.review_key)}"
              >
                查看
              </button>

            </td>

          </tr>
        `,
    )
    .join("");
}

/* ============================================================
   Positive Table
============================================================ */

function applyPositiveFilters() {
  const keyword = $("positiveSearch").value.trim().toLowerCase();

  const category = $("positiveCategoryFilter").value;

  const feature = $("positiveFeatureFilter").value;

  state.positiveFiltered = getPositiveItems().filter((item) => {
    if (keyword) {
      const haystack =
        `${item.final_cluster_name || ""} ${item.final_cluster_id || ""}`.toLowerCase();

      if (!haystack.includes(keyword)) {
        return false;
      }
    }

    if (category && !String(item.issue_categories || "").includes(category)) {
      return false;
    }

    if (feature && !String(item.issue_features || "").includes(feature)) {
      return false;
    }

    return true;
  });

  renderPositiveTable();
}

function renderPositiveTable() {
  $("positiveResultCount").textContent = `${state.positiveFiltered.length} 条`;

  $("positiveEmpty").hidden = state.positiveFiltered.length > 0;

  $("positiveTableBody").innerHTML = state.positiveFiltered
    .map(
      (item) => `
          <tr>

            <td>

              <div class="cluster-name">
                ${escapeHtml(item.final_cluster_name || "—")}
              </div>

              <div class="cluster-id">
                ${escapeHtml(item.final_cluster_id || "")}
              </div>

            </td>

            <td class="cell-muted">
              ${escapeHtml(item.issue_categories || "—")}
            </td>

            <td class="cell-muted">
              ${escapeHtml(item.issue_features || "—")}
            </td>

            <td>
              ${escapeHtml(item.feedback_count || 0)}
            </td>

            <td>

              <button
                class="detail-button insight-detail-button"
                data-review-key="${escapeHtml(item.review_key)}"
              >
                查看
              </button>

            </td>

          </tr>
        `,
    )
    .join("");
}

/* ============================================================
   Positive Report
============================================================ */

function renderPositiveReport() {
  $("positiveReportLoading").hidden = true;

  const report = state.positiveReport;

  if (!report) {
    $("positiveReportEmpty").hidden = false;
    const waiting = positiveReportWaitMessage();
    $("positiveReportEmpty").textContent = waiting || "暂无 Positive Overall Report";
    $("positiveReportContent").hidden = true;
    return;
  }

  $("positiveReportEmpty").hidden = true;

  $("positiveReportContent").hidden = false;

  $("positiveReportMeta").innerHTML = `

    <span class="report-meta-chip">

      ${escapeHtml(report.positive_cluster_count ?? 0)}
      Clusters

    </span>


    <span class="report-meta-chip">

      ${escapeHtml(report.positive_feedback_count ?? 0)}
      Feedback

    </span>


    <span class="report-meta-chip neutral">

      ${escapeHtml(report.validation_status || "")}

    </span>


    <span class="report-meta-chip neutral">

      ${escapeHtml(formatDate(report.generated_at))}

    </span>
  `;

  $("positiveOverallSummary").textContent = report.overall_summary || "—";

  renderCoreStrengths(report.core_strengths || []);

  renderPatterns(report.cross_cluster_patterns || []);

  $("positiveProductImplication").textContent =
    report.product_implication || "—";

  renderSignals(report.representative_signals || []);

  $("positiveLimitations").textContent = report.limitations || "—";
}

function renderCoreStrengths(strengths) {
  $("coreStrengths").innerHTML = strengths
    .map((strength) => {
      const ids = Array.isArray(strength.supporting_cluster_ids)
        ? strength.supporting_cluster_ids
        : [];

      return `
            <article class="strength-card">

              <h4>
                ${escapeHtml(strength.strength_name || "—")}
              </h4>

              <div class="strength-summary">

                ${escapeHtml(strength.summary || "")}

              </div>


              <div class="strength-value">

                <span class="strength-value-label">
                  User Value
                </span>

                ${escapeHtml(strength.user_value || "")}

              </div>


              <div class="cluster-chip-row">

                ${ids
                  .map(
                    (id) => `
                      <span class="cluster-chip">
                        ${escapeHtml(id)}
                      </span>
                    `,
                  )
                  .join("")}

              </div>

            </article>
          `;
    })
    .join("");
}

function renderPatterns(patterns) {
  $("crossClusterPatterns").innerHTML = patterns
    .map(
      (pattern, index) => `
          <div class="pattern-item">

            <span class="pattern-number">
              ${index + 1}
            </span>

            ${escapeHtml(pattern)}

          </div>
        `,
    )
    .join("");
}

function renderSignals(signals) {
  $("representativeSignals").innerHTML = signals
    .map(
      (signal) => `
          <div class="signal-card">

            <div class="signal-cluster">
              ${escapeHtml(signal.final_cluster_id || "")}
            </div>

            <div class="signal-comment">
              ${escapeHtml(signal.comment || "")}
            </div>

          </div>
        `,
    )
    .join("");
}

/* ============================================================
   Drawer
============================================================ */

function openDrawer(reviewKey) {
  const item = state.items.find((item) => item.review_key === reviewKey);

  if (!item) {
    return;
  }

  state.currentItem = item;

  const problem = item.insight_type === "problem";

  $("drawerClusterId").textContent = `${item.final_cluster_id || ""} · ${
    problem ? "Problem" : "Positive"
  }`;

  $("drawerTitle").textContent = item.final_cluster_name || "Untitled Cluster";

  renderDrawerMeta(item);

  $("representativeComment").textContent =
    item.representative_comment || "暂无代表反馈";

  $("aiSummary").textContent = item.insight_summary || "—";

  $("aiNeed").textContent = item.insight_user_need || "—";

  $("aiImplication").textContent = item.product_implication || "—";

  $("aiFollowup").textContent = item.pm_followup || "—";

  $("editSummary").value = item.pm_insight_summary || "";

  $("editNeed").value = item.pm_insight_user_need || "";

  $("editImplication").value = item.pm_product_implication || "";

  $("editFollowup").value = item.pm_followup_edited || "";

  $("reviewComment").value = item.review_comment || "";

  $("lastReviewedBy").textContent = item.reviewed_by || "—";

  $("lastReviewedAt").textContent = formatDate(item.reviewed_at);

  $("pmEditSection").hidden = !problem;

  $("reviewNoteSection").hidden = !problem;

  $("reviewMetaSection").hidden = !problem;

  $("reviewActions").hidden = !problem;

  updateDrawerPosition();

  $("drawer").classList.add("open");

  $("drawerMask").classList.add("open");
}

function renderDrawerMeta(item) {
  let html = "";

  if (item.insight_type === "problem") {
    html += `
      <span class="badge ${priorityClass(item.priority_level)}">
        ${escapeHtml(displayPriority(item))}
      </span>
    `;

    html += `
      <span class="badge ${statusClass(item.review_status)}">
        ${escapeHtml(statusText(item.review_status))}
      </span>
    `;
  } else {
    html += `
      <span class="badge type-positive">
        Positive
      </span>
    `;
  }

  html += `
    <span class="badge priority-excluded">
      ${escapeHtml(item.feedback_count || 0)}
      条反馈
    </span>
  `;

  html += `
    <span class="badge priority-excluded">
      ${escapeHtml(item.issue_categories || "—")}
    </span>
  `;

  html += `
    <span class="badge priority-excluded">
      ${escapeHtml(item.issue_features || "—")}
    </span>
  `;

  $("drawerMeta").innerHTML = html;
}

function closeDrawer() {
  $("drawer").classList.remove("open");

  $("drawerMask").classList.remove("open");

  state.currentItem = null;
}

/* ============================================================
   Drawer Navigation
============================================================ */

function getDrawerCollection() {
  if (!state.currentItem) {
    return [];
  }

  return state.currentItem.insight_type === "problem"
    ? state.problemFiltered
    : state.positiveFiltered;
}

function updateDrawerPosition() {
  const rows = getDrawerCollection();

  if (!state.currentItem) {
    $("drawerPosition").textContent = "";

    return;
  }

  const index = rows.findIndex(
    (item) => item.review_key === state.currentItem.review_key,
  );

  if (index < 0) {
    $("drawerPosition").textContent = "";

    return;
  }

  $("drawerPosition").textContent = `${index + 1} / ${rows.length}`;
}

function goRelative(offset) {
  const rows = getDrawerCollection();

  if (!state.currentItem) {
    return;
  }

  const index = rows.findIndex(
    (item) => item.review_key === state.currentItem.review_key,
  );

  const next = index + offset;

  if (next < 0 || next >= rows.length) {
    return;
  }

  openDrawer(rows[next].review_key);
}

/* ============================================================
   Copy AI
============================================================ */

function copyAiToPm() {
  const item = state.currentItem;

  if (!item || item.insight_type !== "problem") {
    return;
  }

  $("editSummary").value = item.insight_summary || "";

  $("editNeed").value = item.insight_user_need || "";

  $("editImplication").value = item.product_implication || "";

  $("editFollowup").value = item.pm_followup || "";

  showToast("已复制 AI 原文", "success");
}

/* ============================================================
   Submit Review
============================================================ */

async function submitReview(reviewStatus) {
  const item = state.currentItem;

  if (!item || item.insight_type !== "problem") {
    return;
  }

  const reviewer = $("reviewerInput").value.trim();

  if (!reviewer) {
    showToast("请填写 Reviewer", "error");

    $("reviewerInput").focus();

    return;
  }

  const reviewComment = $("reviewComment").value.trim();

  if (reviewStatus === "rejected" && !reviewComment) {
    showToast("驳回时必须填写审核意见", "error");

    return;
  }

  const payload = {
    review_key: item.review_key,

    review_status: reviewStatus,

    review_comment: reviewComment,

    reviewed_by: reviewer,
  };

  if (reviewStatus === "modified") {
    payload.pm_insight_summary = $("editSummary").value.trim();

    payload.pm_insight_user_need = $("editNeed").value.trim();

    payload.pm_product_implication = $("editImplication").value.trim();

    payload.pm_followup_edited = $("editFollowup").value.trim();

    if (
      !payload.pm_insight_summary ||
      !payload.pm_insight_user_need ||
      !payload.pm_product_implication ||
      !payload.pm_followup_edited
    ) {
      showToast("请填写完整 PM 修改版，或先复制 AI 原文", "error");

      return;
    }

    if (!hasPmChanges(item, payload)) {
      showToast("内容与 AI 原文相同，如无需修改请点击“通过”", "error");

      return;
    }
  }

  document.body.classList.add("saving");

  try {
    const params = new URLSearchParams();

    Object.entries(payload).forEach(([key, value]) => {
      params.append(key, value ?? "");
    });

    const response = await fetch(SUBMIT_URL, {
      method: "POST",

      body: params,
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const result = await response.json().catch(() => ({}));

    updateLocalReview(item, payload, result);

    renderProblemStats();

    applyProblemFilters();

    renderDashboard();

    if (reviewStatus === "approved") {
      showToast("审核已通过", "success");
    } else if (reviewStatus === "modified") {
      showToast("PM 修改已保存", "success");
    } else {
      showToast("Insight 已驳回", "success");
    }

    openNextPending(item.review_key);
  } catch (error) {
    console.error(error);

    showToast(`保存失败：${error.message}`, "error");
  } finally {
    document.body.classList.remove("saving");
  }
}

function hasPmChanges(item, payload) {
  return (
    String(payload.pm_insight_summary) !== String(item.insight_summary || "") ||
    String(payload.pm_insight_user_need) !==
      String(item.insight_user_need || "") ||
    String(payload.pm_product_implication) !==
      String(item.product_implication || "") ||
    String(payload.pm_followup_edited) !== String(item.pm_followup || "")
  );
}

function updateLocalReview(item, payload, result) {
  item.review_status = payload.review_status;

  item.review_comment = payload.review_comment;

  item.reviewed_by = payload.reviewed_by;

  item.reviewed_at =
    result?.data?.reviewed_at ||
    result?.reviewed_at ||
    new Date().toISOString();

  if (payload.review_status === "modified") {
    item.pm_insight_summary = payload.pm_insight_summary;

    item.pm_insight_user_need = payload.pm_insight_user_need;

    item.pm_product_implication = payload.pm_product_implication;

    item.pm_followup_edited = payload.pm_followup_edited;
  }
}

function openNextPending(previousKey) {
  const next = state.problemFiltered.find(
    (item) =>
      item.review_status === "pending" && item.review_key !== previousKey,
  );

  if (next) {
    setTimeout(() => {
      openDrawer(next.review_key);
    }, 100);

    return;
  }

  closeDrawer();

  const remaining = getProblemItems().filter(
    (item) => item.review_status === "pending",
  ).length;

  if (remaining === 0) {
    showToast("Problem 已全部审核完成", "success");
  }
}

/* ============================================================
   Sidebar
============================================================ */

function initSidebar() {
  const saved = localStorage.getItem("product-insight-sidebar");

  if (saved === "collapsed") {
    document.body.classList.add("sidebar-collapsed");
  }

  updateSidebarToggle();
}

function toggleSidebar() {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");

  localStorage.setItem(
    "product-insight-sidebar",
    collapsed ? "collapsed" : "expanded",
  );

  updateSidebarToggle();
}

function updateSidebarToggle() {
  const button = $("sidebarToggle");

  if (!button) {
    return;
  }

  const collapsed = document.body.classList.contains("sidebar-collapsed");

  button.textContent = collapsed ? "›" : "‹";

  button.title = collapsed ? "展开侧栏" : "收起侧栏";

  button.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
}

/* ============================================================
   Theme / Reviewer
============================================================ */

function initTheme() {
  const saved = localStorage.getItem("product-insight-theme");

  if (saved === "dark") {
    document.documentElement.classList.add("dark");
  }
}

function toggleTheme() {
  const dark = document.documentElement.classList.toggle("dark");

  localStorage.setItem(
    "product-insight-theme",

    dark ? "dark" : "light",
  );
}

function initReviewer() {
  $("reviewerInput").value = localStorage.getItem("pm-review-reviewer") || "";
}

/* ============================================================
   Events
============================================================ */

$("sidebarToggle").addEventListener("click", toggleSidebar);
document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    navigatePage(button.dataset.page);
  });
});

$("goProblemBtn").addEventListener("click", () => {
  navigatePage("problem");
});

$("goPositiveBtn").addEventListener("click", () => {
  navigatePage("positive");
});

$("refreshBtn").addEventListener("click", loadEverything);

$("retryBtn").addEventListener("click", loadEverything);

$("themeBtn").addEventListener("click", toggleTheme);

$("batchSelect").addEventListener("change", (event) => {
  if (event.target.value === BATCH_ADD_OPTION) {
    showBatchAddForm();
  } else {
    selectBatch(event.target.value);
  }
});

$("batchAddForm").addEventListener("submit", handleBatchAdd);
$("analysisForm").addEventListener("submit", handleStartAnalysis);
$("analysisStopWaitBtn").addEventListener("click", stopAnalysisWaiting);

$("batchCancelBtn").addEventListener("click", () => {
  $("batchAddForm").hidden = true;
  $("batchSelect").value = state.selectedRunId;
});

$("reviewerInput").addEventListener("input", () => {
  localStorage.setItem("pm-review-reviewer", $("reviewerInput").value);
});

[
  "problemSearch",
  "problemStatusFilter",
  "problemPriorityFilter",
  "problemCategoryFilter",
  "problemFeatureFilter",
].forEach((id) => {
  $(id).addEventListener(
    id === "problemSearch" ? "input" : "change",
    applyProblemFilters,
  );
});

["positiveSearch", "positiveCategoryFilter", "positiveFeatureFilter"].forEach(
  (id) => {
    $(id).addEventListener(
      id === "positiveSearch" ? "input" : "change",
      applyPositiveFilters,
    );
  },
);

document.addEventListener("click", (event) => {
  const insightButton = event.target.closest(".insight-detail-button");

  if (insightButton) {
    openDrawer(insightButton.dataset.reviewKey);

    return;
  }

  const dashboardButton = event.target.closest(".dashboard-problem-detail");

  if (dashboardButton) {
    navigatePage("problem");

    openDrawer(dashboardButton.dataset.reviewKey);
  }
});

$("drawerClose").addEventListener("click", closeDrawer);

$("drawerMask").addEventListener("click", closeDrawer);

$("prevBtn").addEventListener("click", () => {
  goRelative(-1);
});

$("nextBtn").addEventListener("click", () => {
  goRelative(1);
});

$("copyAiBtn").addEventListener("click", copyAiToPm);

$("approveBtn").addEventListener("click", () => {
  submitReview("approved");
});

$("modifyBtn").addEventListener("click", () => {
  submitReview("modified");
});

$("rejectBtn").addEventListener("click", () => {
  submitReview("rejected");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeDrawer();
  }
});

/* ============================================================
   Init
============================================================ */

async function init() {
  initTheme();

  initSidebar();

  initReviewer();

  initBatchSelector();

  restoreAnalysisWaiting();

  const hashPage = location.hash.replace("#", "");
  navigatePage(pageConfig[hashPage] ? hashPage : "dashboard", false);

  try {
    await refreshBatchListFromApi();
  } catch (error) {
    console.error("读取已入库批次列表失败：", error);
    state.customBatches = [];
    state.selectedRunId = "";
    localStorage.removeItem(BATCH_SELECTED_KEY);
    renderBatchSelect();
    if (!analysisPollingRunId) {
      showAnalysisStatus(
        "暂时无法读取已入库批次列表。请确认 n8n 主工作流已导入并发布最新版，然后刷新页面。",
        "error",
      );
    }
  }

  await loadEverything();
}

init();
