'use strict';

const http = require('node:http');
const https = require('node:https');
const { spawn } = require('node:child_process');

const READINESS_ATTEMPTS = 30;
const REQUEST_TIMEOUT_MS = 5000;
const DEFAULT_RETRY_DELAY_MS = 2000;
const apiKey = process.env.N8N_API_KEY || '';
const apiBaseUrl = process.env.N8N_API_URL || '';

function fail(message) {
  console.error(`[n8n-mcp] ${message}`);
  process.exitCode = 1;
}

function retryDelayMs() {
  const configured = Number.parseInt(
    process.env.N8N_MCP_READINESS_DELAY_MS || '',
    10,
  );
  if (Number.isFinite(configured) && configured >= 0 && configured <= 10000) {
    return configured;
  }
  return DEFAULT_RETRY_DELAY_MS;
}

function apiEndpoint() {
  if (!apiKey) {
    throw new Error('N8N_API_KEY is required before n8n-mcp can start');
  }
  if (!apiBaseUrl) {
    throw new Error('N8N_API_URL is required before n8n-mcp can start');
  }

  const parsed = new URL(apiBaseUrl);
  if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) {
    throw new Error('N8N_API_URL must be an HTTP(S) URL');
  }
  if (parsed.username || parsed.password) {
    throw new Error('N8N_API_URL must not contain credentials');
  }
  parsed.pathname = '/api/v1/workflows';
  parsed.search = '?limit=1';
  parsed.hash = '';
  return parsed;
}

function requestN8n(endpoint) {
  return new Promise((resolve) => {
    const transport = endpoint.protocol === 'https:' ? https : http;
    const request = transport.request(
      endpoint,
      {
        method: 'GET',
        headers: {
          Accept: 'application/json',
          'X-N8N-API-KEY': apiKey,
        },
      },
      (response) => {
        const statusCode = response.statusCode || 0;
        response.resume();
        response.on('end', () => resolve({ statusCode, networkError: false }));
      },
    );

    request.setTimeout(REQUEST_TIMEOUT_MS, () => {
      request.destroy();
      resolve({ statusCode: 0, networkError: true });
    });
    request.on('error', () => resolve({ statusCode: 0, networkError: true }));
    request.end();
  });
}

function waitForRetry() {
  return new Promise((resolve) => setTimeout(resolve, retryDelayMs()));
}

async function validateApiKey() {
  let endpoint;
  try {
    endpoint = apiEndpoint();
  } catch (error) {
    fail(error.message);
    return false;
  }

  for (let attempt = 1; attempt <= READINESS_ATTEMPTS; attempt += 1) {
    const result = await requestN8n(endpoint);
    if (result.statusCode >= 200 && result.statusCode < 300) {
      return true;
    }

    if (result.statusCode === 401 || result.statusCode === 403) {
      fail(`authoritative n8n API rejected N8N_API_KEY (HTTP ${result.statusCode})`);
      return false;
    }

    if (attempt < READINESS_ATTEMPTS) {
      await waitForRetry();
      continue;
    }

    if (result.networkError) {
      fail('n8n API remained unreachable during readiness checks');
    } else {
      fail(`n8n API readiness check failed (HTTP ${result.statusCode || 'unknown'})`);
    }
    return false;
  }

  return false;
}

function startOriginalMcpCommand() {
  const originalEntrypoint =
    process.env.N8N_MCP_ORIGINAL_ENTRYPOINT || '/usr/local/bin/docker-entrypoint.sh';
  const child = spawn(originalEntrypoint, process.argv.slice(2), {
    env: process.env,
    stdio: 'inherit',
  });

  child.on('error', (error) => {
    fail(`could not start the original n8n-mcp entrypoint (${error.code || 'error'})`);
  });
  child.on('exit', (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code === null ? 1 : code;
  });
}

(async () => {
  if (await validateApiKey()) {
    startOriginalMcpCommand();
  }
})();
