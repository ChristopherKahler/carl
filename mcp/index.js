#!/usr/bin/env node
/**
 * CARL MCP — Consolidated Tool Server
 * Context Augmentation & Reinforcement Layer
 *
 * Consolidates: DRL-engine + decision-logger + Staging + carl-json (v2)
 * 4 tool groups, all optional, activated by CARL rules.
 *
 * When installed to .carl/carl-mcp/, workspace path resolves to parent of .carl/
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import path from 'path';
import { fileURLToPath } from 'url';

// Tool group imports
import { TOOLS as domainTools, handleTool as handleDomain } from './tools/domains.js';
import { TOOLS as decisionTools, handleTool as handleDecision } from './tools/decisions.js';
import { TOOLS as stagingTools, handleTool as handleStaging } from './tools/staging.js';
import { TOOLS as carlJsonTools, handleTool as handleCarlJson } from './tools/carl-json.js';

// ============================================================
// CONFIGURATION
// ============================================================

// Resolve workspace from this file's location:
// Installed at .carl/carl-mcp/index.js → workspace is ../..
// Prefer CLAUDE_PROJECT_DIR when set so the MCP server anchors to the
// correct project root regardless of where node was invoked from.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_PATH = (process.env.CLAUDE_PROJECT_DIR && process.env.CLAUDE_PROJECT_DIR.trim())
    ? path.resolve(process.env.CLAUDE_PROJECT_DIR.trim()) : path.resolve(__dirname, '../..');

function debugLog(...args) {
    console.error('[CARL]', new Date().toISOString(), ...args);
}

// ============================================================
// TOOL REGISTRY
// ============================================================

const ALL_TOOLS = [...domainTools, ...decisionTools, ...stagingTools, ...carlJsonTools];

// Build handler lookup: tool name → handler function
const TOOL_HANDLERS = {};
for (const tool of domainTools) TOOL_HANDLERS[tool.name] = handleDomain;
for (const tool of decisionTools) TOOL_HANDLERS[tool.name] = handleDecision;
for (const tool of stagingTools) TOOL_HANDLERS[tool.name] = handleStaging;
for (const tool of carlJsonTools) TOOL_HANDLERS[tool.name] = handleCarlJson;

// ============================================================
// MCP SERVER
// ============================================================

const server = new Server({
    name: "carl-mcp",
    version: "2.0.0",
}, {
    capabilities: {
        tools: {},
    },
});

debugLog('CARL MCP Server initialized');
debugLog('Workspace:', WORKSPACE_PATH);
debugLog('Tool groups: domains (%d), decisions (%d), staging (%d), carl-json (%d)',
    domainTools.length, decisionTools.length, stagingTools.length, carlJsonTools.length);
debugLog('Total tools:', ALL_TOOLS.length);

server.setRequestHandler(ListToolsRequestSchema, async () => {
    debugLog('List tools request');
    return { tools: ALL_TOOLS };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    debugLog('Call tool:', name);

    try {
        const handler = TOOL_HANDLERS[name];
        if (!handler) {
            throw new Error(`Unknown tool: ${name}`);
        }

        const result = await handler(name, args || {}, WORKSPACE_PATH);

        if (result === null) {
            throw new Error(`Tool ${name} returned null — handler mismatch`);
        }

        return {
            content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
            isError: false,
        };
    } catch (error) {
        debugLog('Error:', error.message);
        return {
            content: [{ type: "text", text: `Error: ${error.message}` }],
            isError: true,
        };
    }
});

// ============================================================
// RUN
// ============================================================

async function runServer() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error("CARL MCP Server running on stdio");
}

try {
    await runServer();
} catch (error) {
    console.error("Fatal error:", error);
    process.exit(1);
}
