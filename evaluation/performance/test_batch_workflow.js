// Offline smoke tests for the CURRENT exported n8n workflow.
// No network requests, no n8n executions, and no DeepSeek API calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const workflowPath = path.resolve(__dirname, "../../Doubao - Dynamic Analysis Test .json");
const workflow = JSON.parse(fs.readFileSync(workflowPath, "utf8"));
const nodes = new Map(workflow.nodes.map(node => [node.name, node]));

function runCodeNode(name, inputItems, refs = {}) {
  const node = nodes.get(name);
  assert.ok(node && typeof node.parameters?.jsCode === "string", `Missing Code node: ${name}`);
  const fn = new Function("$input", "$items", node.parameters.jsCode);
  return fn(
    { all: () => inputItems },
    referencedName => refs[referencedName] ?? []
  );
}

// n8n exports both ={{ expression }} and =\n{{ expression }} syntax.
// Extract the WHOLE expression instead of using fixed slice(3, -2) offsets.
function evaluateJsonBody(nodeName, json) {
  const node = nodes.get(nodeName);
  assert.ok(node, `Missing HTTP node: ${nodeName}`);
  const value = node.parameters?.jsonBody;
  assert.equal(typeof value, "string", `${nodeName}: missing JSON Body expression`);
  const match = /^=\s*\{\{([\s\S]*?)\}\}\s*$/.exec(value);
  assert.ok(match, `${nodeName}: expected a complete n8n expression`);
  const evaluated = new Function("$json", `"use strict"; return (${match[1].trim()});`)(json);
  const request = typeof evaluated === "string" ? JSON.parse(evaluated) : evaluated;
  assert.ok(request && typeof request === "object" && !Array.isArray(request), `${nodeName}: JSON Body is not an object`);
  // Round-trip to ensure the object can be serialized as an HTTP JSON request.
  return JSON.parse(JSON.stringify(request));
}

function assertBatchHttp(nodeName, reviews) {
  const request = evaluateJsonBody(nodeName, { reviews });
  assert.equal(request.model, "deepseek-flash");
  assert.deepEqual(request.response_format, { type: "json_object" });
  assert.ok(Array.isArray(request.messages));
  assert.ok(request.messages.some(msg => msg.role === "system" && typeof msg.content === "string"));
  const user = request.messages.find(msg => msg.role === "user");
  assert.ok(user && typeof user.content === "string", `${nodeName}: missing user content`);
  const match = /<reviews>\s*([\s\S]*?)\s*<\/reviews>/.exec(user.content);
  assert.ok(match, `${nodeName}: missing <reviews> JSON block`);
  const embeddedReviews = JSON.parse(match[1]);
  assert.equal(embeddedReviews.length, reviews.length);
  assert.deepEqual(embeddedReviews.map(row => row.batch_item_id), reviews.map(row => row.batch_item_id));
}

function mockResponse(content) {
  return {
    json: {
      model: "deepseek-flash",
      choices: [{ finish_reason: "stop", message: { content: JSON.stringify(content) } }]
    }
  };
}

function runScenario(count, invalidSourceIds = new Set()) {
  const sourceReviews = Array.from({ length: count }, (_, index) => ({
    json: {
      source_comment_id: String(1001 + index),
      content: `离线模拟评论 ${index + 1}`,
      rating: 5,
      review_date: "2026-10-30 10:00:00",
      source: "应用宝",
      app_version: null
    }
  }));
  const qualityBatches = runCodeNode("Build Quality Batches", sourceReviews);
  assert.deepEqual(qualityBatches.map(batch => batch.json.reviews.length), [3, 3, 3, 1]);
  qualityBatches.forEach(batch => assertBatchHttp("DeepSeek - Quality Filter Batch", batch.json.reviews));

  const qualityResponses = qualityBatches.map(batch => mockResponse({
    results: batch.json.reviews.map(review => {
      const is_valid = !invalidSourceIds.has(String(review.source_comment_id));
      return { batch_item_id: review.batch_item_id, is_valid,
        invalid_reason: is_valid ? null : "缺少具体产品信息" };
    })
  }));
  const qualityRows = runCodeNode("Parse Quality Batch Result", qualityResponses,
    { "Build Quality Batches": qualityBatches });
  assert.equal(qualityRows.length, count);
  assert.ok(qualityRows.every(row => row.json.parse_status === "success"));
  assert.deepEqual(qualityRows.map(row => row.json.source_comment_id),
    sourceReviews.map(row => row.json.source_comment_id));

  const validRows = qualityRows.filter(row => row.json.is_valid === true);
  assert.equal(validRows.length, count - invalidSourceIds.size);
  const analyzerBatches = runCodeNode("Build Analyzer Batches", validRows);
  assert.deepEqual(analyzerBatches.map(batch => batch.json.reviews.length),
    invalidSourceIds.size ? [2, 2, 2, 2, 1] : [2, 2, 2, 2, 2]);
  analyzerBatches.forEach(batch => assertBatchHttp("DeepSeek - Feedback Analyzer Batch", batch.json.reviews));

  const analyzerResponses = analyzerBatches.map(batch => mockResponse({
    results: batch.json.reviews.map(review => ({
      batch_item_id: review.batch_item_id,
      issues: [{
        category: "功能异常", feature: "App基础体验", sentiment: "negative",
        severity: 3, user_need: "正常使用功能",
        summary: `对应评论 ${review.source_comment_id}`, confidence: 0.9
      }]
    }))
  }));
  const analyzerRows = runCodeNode("Parse Analyzer Batch Result", analyzerResponses,
    { "Build Analyzer Batches": analyzerBatches });
  assert.equal(analyzerRows.length, validRows.length);
  assert.ok(analyzerRows.every(row => row.json.parse_status === "success"));
  assert.deepEqual(analyzerRows.map(row => row.json.source_comment_id),
    validRows.map(row => row.json.source_comment_id));
  assert.deepEqual(analyzerRows.map(row => row.json.summary),
    validRows.map(row => `对应评论 ${row.json.source_comment_id}`));
  return { qualityBatches: qualityBatches.length, analyzerBatches: analyzerBatches.length,
    qualityRows: qualityRows.length, analyzerRows: analyzerRows.length };
}

const allValid = runScenario(10);
const oneInvalid = runScenario(10, new Set(["1003"]));
console.log("Offline batch tests passed:", JSON.stringify({ allValid, oneInvalid }));
