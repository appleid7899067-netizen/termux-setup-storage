#!/usr/bin/env node
// Run Puter.js with the user's browser-authenticated token and expose a tiny local adapter.
const http = require('node:http');
const { createRequire } = require('node:module');
const { readFile } = require('node:fs/promises');
const { join } = require('node:path');
const requireFromProject = createRequire(join(__dirname, 'package.json'));
const { init } = requireFromProject('@heyputer/puter.js/src/init.cjs');

const tokenFile = join(process.env.HOME || '/data/data/com.termux/files/home', '.codex', 'puter-auth-token');
let puter;

function reply(res, status, value) {
  const body = Buffer.from(JSON.stringify(value));
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': body.length });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url.split('?')[0] === '/models') {
      const models = await puter.ai.listModels();
      return reply(res, 200, { data: models.filter(model => model && model.id).map(model => ({ id: model.id, name: model.name || model.id, provider: model.provider || '' })) });
    }
    if (req.method !== 'POST' || req.url !== '/chat') return reply(res, 404, { error: { message: 'Not found' } });
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const options = { model: input.model, stream: false, normalize: true };
    for (const key of ['tools', 'tool_choice', 'temperature', 'max_tokens', 'reasoning_effort']) {
      if (input[key] !== undefined) options[key] = input[key];
    }
    const messages = (input.messages || []).filter(message => message && typeof message === 'object').map(message => ({
      ...message,
      role: ['system', 'user', 'assistant', 'tool'].includes(message.role) ? message.role : 'user',
      content: message.content == null ? '' : String(message.content),
    }));
    const result = await puter.ai.chat(messages, options);
    const message = result.message || { role: 'assistant', content: '' };
    return reply(res, 200, {
      id: result.id || `puter-${Date.now()}`,
      object: 'chat.completion',
      created: Math.floor(Date.now() / 1000),
      model: input.model,
      choices: [{ index: 0, message, finish_reason: result.finish_reason || (message.tool_calls?.length ? 'tool_calls' : 'stop') }],
      usage: result.usage || {},
    });
  } catch (error) {
    const status = Number(error.status || error.statusCode || 502);
    const body = error.response || error.body;
    return reply(res, status >= 400 && status <= 599 ? status : 502, {
      error: {
        message: error.message || String(error),
        type: error.type || 'puter_sdk_error',
        code: error.code || null,
        details: body || undefined,
      },
    });
  }
});

(async () => {
  try {
    const token = (await readFile(tokenFile, 'utf8')).trim();
    if (!token) throw new Error('Puter token is empty');
    puter = init(token);
    server.listen(0, '127.0.0.1', () => {
      console.log(`http://127.0.0.1:${server.address().port}`);
    });
  } catch (error) {
    console.error(`Puter SDK startup failed: ${error.message}`);
    process.exitCode = 1;
  }
})();
