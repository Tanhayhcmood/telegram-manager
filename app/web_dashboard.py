"""Read-only web dashboard backed by the same data used by the Telegram bot."""

import asyncio
import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.config import settings
from app.database.connection import AsyncSessionLocal
from app.repositories import GroupRepository, LogRepository
from app.services.health_service import HealthService
from app.services.stats_service import StatsService
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _group_to_dict(group: Any) -> dict[str, Any]:
    return {
        "id": group.group_id,
        "title": group.title or "بدون عنوان",
        "username": group.username,
        "members_count": group.members_count,
        "status": group.status.value,
        "can_write": group.can_write,
        "join_date": _iso(group.join_date),
        "created_at": _iso(group.created_at),
        "updated_at": _iso(group.updated_at),
    }


def _log_to_dict(log: Any) -> dict[str, Any]:
    return {
        "timestamp": _iso(log.timestamp),
        "action": log.action,
        "result": log.result,
        "error_message": log.error_message,
        "actor": log.actor,
        "target": log.target,
        "details": log.details,
    }


async def build_dashboard_payload() -> dict[str, Any]:
    """Build one authoritative snapshot for every dashboard request."""
    stats = await StatsService().get_stats()
    health = HealthService.get_instance()

    async with AsyncSessionLocal() as session:
        group_repo = GroupRepository(session)
        log_repo = LogRepository(session)
        groups = await group_repo.get_latest(limit=100)
        recent_activity = await log_repo.get_recent(limit=20)
        recent_errors = await log_repo.get_errors(limit=10)

    stats_data = asdict(stats)
    stats_data["generated_at"] = _iso(stats.generated_at)
    stats_data["last_activity"] = _iso(stats.last_activity)
    stats_data["live_refreshed_at"] = _iso(stats.live_refreshed_at)

    consistency_known = stats.live_group_count is not None
    consistency_difference = (
        stats.live_group_count - stats.joined_groups
        if consistency_known
        else None
    )

    return {
        "generated_at": _iso(stats.generated_at),
        "data_source": "database + Telegram live snapshot",
        "consistency": {
            "known": consistency_known,
            "synchronized": consistency_known and consistency_difference == 0,
            "live_group_count": stats.live_group_count,
            "database_joined_count": stats.joined_groups,
            "difference": consistency_difference,
            "message": (
                "داده‌های Telegram و دیتابیس هماهنگ هستند"
                if consistency_known and consistency_difference == 0
                else "بین snapshot زنده Telegram و دیتابیس اختلاف وجود دارد"
                if consistency_known
                else "snapshot زنده Telegram در دسترس نیست"
            ),
        },
        "stats": stats_data,
        "health": {
            "client_healthy": stats.client_healthy,
            "last_ok_at": _iso(health.last_ok_at()),
        },
        "groups": [_group_to_dict(group) for group in groups],
        "recent_activity": [_log_to_dict(log) for log in recent_activity],
        "recent_errors": [_log_to_dict(log) for log in recent_errors],
    }


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>پنل مدیریت گروه‌های Telegram</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1020;
      --panel: #121a2c;
      --panel-2: #18243b;
      --line: #283855;
      --text: #eef4ff;
      --muted: #9eacc4;
      --blue: #5ca8ff;
      --green: #43d19b;
      --yellow: #f2c96b;
      --red: #ff7676;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top right, #1b2f55 0, var(--bg) 42rem);
      color: var(--text);
      font-family: Tahoma, Arial, sans-serif;
    }
    .shell { max-width: 1380px; margin: 0 auto; padding: 28px 18px 48px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; margin-bottom: 24px; }
    h1, h2, p { margin: 0; }
    h1 { font-size: clamp(1.35rem, 3vw, 2.2rem); letter-spacing: -.03em; }
    h2 { font-size: 1rem; margin-bottom: 14px; }
    .subtitle { color: var(--muted); margin-top: 8px; font-size: .9rem; }
    .status { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); white-space: nowrap; }
    .dot { width: 10px; height: 10px; border-radius: 99px; background: var(--yellow); box-shadow: 0 0 12px currentColor; }
    .dot.ok { background: var(--green); color: var(--green); }
    .dot.bad { background: var(--red); color: var(--red); }
    .banner { display: none; border: 1px solid #875151; background: #3b2029; color: #ffd8d8; padding: 12px 14px; border-radius: 12px; margin-bottom: 18px; }
    .banner.show { display: block; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .card, .section { background: color-mix(in srgb, var(--panel) 93%, transparent); border: 1px solid var(--line); border-radius: 16px; box-shadow: 0 12px 32px #0002; }
    .card { padding: 18px; }
    .label { color: var(--muted); font-size: .8rem; }
    .value { font-size: 1.8rem; font-weight: 700; margin-top: 8px; direction: ltr; text-align: right; }
    .section { padding: 18px; margin-top: 14px; }
    .section-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
    .consistency { color: var(--muted); font-size: .82rem; }
    .consistency.ok { color: var(--green); }
    .consistency.bad { color: var(--red); }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 650px; }
    th, td { padding: 11px 8px; border-bottom: 1px solid var(--line); text-align: right; font-size: .84rem; }
    th { color: var(--muted); font-weight: 400; }
    td code { direction: ltr; display: inline-block; color: #c8dcff; }
    .pill { display: inline-block; border-radius: 99px; padding: 4px 9px; font-size: .72rem; background: var(--panel-2); }
    .pill.joined { color: var(--green); }
    .pill.pending, .pill.approved { color: var(--yellow); }
    .pill.failed { color: var(--red); }
    .empty { color: var(--muted); padding: 24px 0; text-align: center; }
    .activity { display: grid; gap: 9px; }
    .activity-row { display: flex; justify-content: space-between; gap: 14px; border-bottom: 1px solid var(--line); padding-bottom: 9px; font-size: .82rem; }
    .activity-row:last-child { border-bottom: 0; padding-bottom: 0; }
    .activity-time { color: var(--muted); direction: ltr; white-space: nowrap; }
    footer { color: var(--muted); font-size: .75rem; margin-top: 18px; display: flex; justify-content: space-between; gap: 12px; }
    @media (max-width: 900px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } header { flex-direction: column; } }
    @media (max-width: 520px) { .shell { padding: 20px 12px 36px; } .grid { grid-template-columns: 1fr 1fr; gap: 8px; } .card { padding: 13px; } .value { font-size: 1.4rem; } }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>پنل مدیریت گروه‌های Telegram</h1>
        <p class="subtitle">گزارش واحد از دیتابیس و snapshot زنده Telegram</p>
      </div>
      <div class="status"><span id="status-dot" class="dot"></span><span id="status-text">در حال اتصال…</span></div>
    </header>
    <div id="error-banner" class="banner"></div>
    <section class="grid" aria-label="خلاصه آمار">
      <div class="card"><div class="label">کل گروه‌های ثبت‌شده</div><div id="total-groups" class="value">—</div></div>
      <div class="card"><div class="label">عضویت فعلی در Telegram</div><div id="live-groups" class="value">—</div></div>
      <div class="card"><div class="label">عضویت موفق امروز</div><div id="today-joins" class="value">—</div></div>
      <div class="card"><div class="label">در صف / ناموفق</div><div id="queue-failed" class="value">—</div></div>
    </section>
    <section class="section">
      <div class="section-head"><h2>وضعیت هماهنگی داده‌ها</h2><div id="consistency" class="consistency">—</div></div>
      <p id="consistency-detail" class="subtitle">—</p>
    </section>
    <section class="section">
      <div class="section-head"><h2>گروه‌ها</h2><div class="consistency">فهرست از همان دیتابیس آمار</div></div>
      <div id="groups" class="table-wrap"><div class="empty">در حال دریافت…</div></div>
    </section>
    <section class="section">
      <div class="section-head"><h2>آخرین فعالیت‌ها</h2><div class="consistency">۲۰ رویداد اخیر</div></div>
      <div id="activity" class="activity"><div class="empty">در حال دریافت…</div></div>
    </section>
    <footer><span id="updated">آخرین بروزرسانی: —</span><span>بروزرسانی خودکار هر ۱۰ ثانیه</span></footer>
  </main>
  <script>
    const fa = new Intl.NumberFormat('fa-IR');
    const el = (id) => document.getElementById(id);
    const num = (v) => v === null || v === undefined ? '—' : fa.format(v);
    const when = (v) => v ? new Date(v).toLocaleString('fa-IR', {dateStyle:'short', timeStyle:'medium'}) : '—';
    const labels = {joined:'عضو شده', pending:'در انتظار', approved:'تأیید شده', rejected:'رد شده', failed:'ناموفق', left:'خارج شده'};
    function setStatus(ok, text) {
      el('status-dot').className = 'dot ' + (ok ? 'ok' : 'bad');
      el('status-text').textContent = text;
    }
    function renderGroups(groups) {
      if (!groups.length) { el('groups').innerHTML = '<div class="empty">هنوز گروهی ثبت نشده است.</div>'; return; }
      el('groups').innerHTML = `<table><thead><tr><th>گروه</th><th>شناسه</th><th>وضعیت</th><th>اعضا</th><th>آخرین بروزرسانی</th></tr></thead><tbody>${
        groups.map(g => `<tr><td>${escapeHtml(g.title)}</td><td><code>${escapeHtml(String(g.id))}</code></td><td><span class="pill ${g.status}">${labels[g.status] || g.status}</span></td><td>${num(g.members_count)}</td><td>${when(g.updated_at)}</td></tr>`).join('')
      }</tbody></table>`;
    }
    function renderActivity(rows) {
      if (!rows.length) { el('activity').innerHTML = '<div class="empty">هنوز فعالیتی ثبت نشده است.</div>'; return; }
      el('activity').innerHTML = rows.map(r => `<div class="activity-row"><span>${escapeHtml(r.action || 'رویداد')} ${r.result === 'error' ? '— خطا' : ''}</span><span class="activity-time">${when(r.timestamp)}</span></div>`).join('');
    }
    function escapeHtml(value) {
      return value.replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    async function loadDashboard() {
      try {
        const response = await fetch('/api/dashboard', {cache:'no-store'});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'دریافت گزارش ناموفق بود');
        const s = data.stats;
        el('total-groups').textContent = num(s.total_groups);
        el('live-groups').textContent = num(data.consistency.live_group_count);
        el('today-joins').textContent = num(s.today_joins);
        el('queue-failed').textContent = `${num(s.pending_queue_size)} / ${num(s.failed_groups)}`;
        const c = data.consistency;
        el('consistency').textContent = c.message;
        el('consistency').className = 'consistency ' + (c.synchronized ? 'ok' : c.known ? 'bad' : '');
        el('consistency-detail').textContent = c.known
          ? `عضو شده در دیتابیس: ${num(c.database_joined_count)} — عضو فعلی در Telegram: ${num(c.live_group_count)} — اختلاف: ${num(Math.abs(c.difference))}`
          : 'تا زمان برقراری اتصال User Client، شمارش زنده Telegram قابل تأیید نیست.';
        renderGroups(data.groups || []);
        renderActivity(data.recent_activity || []);
        el('updated').textContent = 'آخرین بروزرسانی: ' + when(data.generated_at);
        el('error-banner').className = 'banner';
        setStatus(data.health.client_healthy, data.health.client_healthy ? 'User Client سالم' : 'User Client نیازمند بررسی');
      } catch (error) {
        el('error-banner').textContent = error.message || 'خطا در دریافت گزارش';
        el('error-banner').className = 'banner show';
        setStatus(false, 'خطا در دریافت داده');
      }
    }
    loadDashboard();
    setInterval(loadDashboard, 10000);
  </script>
</body>
</html>"""


class DashboardServer:
    """Small dependency-free HTTP server for Render and the read-only panel."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._ready = False

    async def start(self, port: int) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", port)
        logger.info("Dashboard server listening on port %d", port)

    def mark_ready(self) -> None:
        self._ready = True

    def close(self) -> None:
        if self._server:
            self._server.close()

    async def wait_closed(self) -> None:
        if self._server:
            await self._server.wait_closed()

    def _authorized(self, headers: dict[str, str]) -> bool:
        expected = settings.DASHBOARD_TOKEN.strip()
        if not expected:
            return True
        return headers.get("authorization", "") == f"Bearer {expected}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line:
                return
            parts = request_line.decode("latin-1").strip().split()
            if len(parts) != 3:
                await self._send(writer, 400, "text/plain; charset=utf-8", "درخواست نامعتبر")
                return
            method, target, _ = parts
            headers: dict[str, str] = {}
            for _ in range(50):
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                if key:
                    headers[key.lower().strip()] = value.strip()

            if method != "GET":
                await self._send(writer, 405, "text/plain; charset=utf-8", "فقط GET پشتیبانی می‌شود")
                return
            if not self._authorized(headers):
                await self._send_json(writer, 401, {"error": "دسترسی به پنل مجاز نیست"})
                return

            parsed = urlsplit(target)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                await self._send(writer, 200, "text/html; charset=utf-8", _dashboard_html())
            elif path in {"/health", "/api/health"}:
                await self._send_json(writer, 200, {
                    "ok": True,
                    "ready": self._ready,
                    "service": "telegram-manager",
                })
            elif path == "/api/dashboard":
                if not self._ready:
                    await self._send_json(writer, 503, {"error": "سرویس هنوز آماده نیست"})
                    return
                try:
                    payload = await build_dashboard_payload()
                    await self._send_json(writer, 200, payload)
                except Exception:
                    logger.exception("Dashboard snapshot failed")
                    await self._send_json(writer, 503, {"error": "دریافت گزارش موقتاً ناموفق بود"})
            elif path == "/api/groups":
                if not self._ready:
                    await self._send_json(writer, 503, {"error": "سرویس هنوز آماده نیست"})
                    return
                query = parse_qs(parsed.query)
                try:
                    limit = min(max(int(query.get("limit", ["100"])[0]), 1), 100)
                except ValueError:
                    await self._send_json(writer, 400, {"error": "پارامتر limit نامعتبر است"})
                    return
                async with AsyncSessionLocal() as session:
                    groups = await GroupRepository(session).get_latest(limit=limit)
                await self._send_json(writer, 200, {"groups": [_group_to_dict(g) for g in groups]})
            else:
                await self._send_json(writer, 404, {"error": "مسیر پیدا نشد"})
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError):
            return
        except Exception:
            logger.exception("Dashboard request failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]) -> None:
        await self._send(
            writer,
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False, default=_json_default),
        )

    async def _send(self, writer: asyncio.StreamWriter, status: int, content_type: str, body: str) -> None:
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed", 503: "Service Unavailable"}.get(status, "Error")
        encoded = body.encode("utf-8")
        response = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1") + encoded
        writer.write(response)
        await writer.drain()