#!/usr/bin/env node
// Termux browser login for Puter. The token is saved privately and never printed.
const http = require('node:http');
const { spawn } = require('node:child_process');
const { existsSync } = require('node:fs');
const { chmod, mkdir, writeFile } = require('node:fs/promises');
const { homedir } = require('node:os');
const { join } = require('node:path');

function openUrl(url) {
  return new Promise((resolve, reject) => {
    const hasTermuxOpen = (process.env.PATH || '').split(':').some(dir => existsSync(join(dir, 'termux-open-url')));
    const command = hasTermuxOpen ? 'termux-open-url' : 'am';
    const args = command === 'am' ? ['start', '-a', 'android.intent.action.VIEW', '-d', url] : [url];
    const child = spawn(command, args, { stdio: 'ignore' });
    child.once('error', reject);
    child.once('exit', code => code === 0 ? resolve() : reject(new Error(`termux-open-url exited ${code}`)));
  });
}

(async () => {
  let server;
  try {
    const token = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('หมดเวลารอการยืนยัน Puter')), 5 * 60 * 1000);
      server = http.createServer((req, res) => {
        const token = new URL(req.url, 'http://localhost').searchParams.get('token');
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end('<!doctype html><meta charset="utf-8"><title>Puter login</title><p>เข้าสู่ระบบสำเร็จ กลับไปที่ Termux ได้เลย</p>');
        if (token) {
          clearTimeout(timer);
          resolve(token);
        }
      });
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.listen(0, '127.0.0.1', async () => {
        const port = server.address().port;
        const redirectURL = encodeURIComponent(`http://127.0.0.1:${port}`);
        try {
          await openUrl(`https://puter.com/?action=authme&redirectURL=${redirectURL}`);
          console.log('กรุณาเข้าสู่ระบบ Puter ใน browser ที่เปิดขึ้น…');
        } catch (error) {
          clearTimeout(timer);
          reject(error);
        }
      });
    });
    const dir = join(homedir(), '.codex');
    await mkdir(dir, { recursive: true, mode: 0o700 });
    const tokenPath = join(dir, 'puter-auth-token');
    await writeFile(tokenPath, `${token}\n`, { mode: 0o600 });
    await chmod(tokenPath, 0o600);
    console.log('เข้าสู่ระบบ Puter สำเร็จ');
  } catch (error) {
    console.error(`Puter login ไม่สำเร็จ: ${error.message}`);
    process.exitCode = 1;
  } finally {
    if (server) server.close();
  }
})();
