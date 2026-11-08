const DUPLICATE_MATCH = `
  p.account_id = 'account_03'
  AND p.created_at_ms > ?
  AND (
    (? <> '' AND p.signal_id = ?)
    OR (
      p.base_asset = ?
      AND p.side = ?
      AND p.timeframe = ?
      AND ABS(p.entry_mid - ?) <= MAX(ABS(p.entry_mid), ABS(?), 1e-12) * 0.01
      AND ABS(p.stop_percent - ?) <= MAX(ABS(p.stop_percent), ABS(?), 1e-12) * 0.20
      AND ABS(p.tp1_percent - ?) <= MAX(ABS(p.tp1_percent), ABS(?), 1e-12) * 0.20
      AND ABS(p.tp2_percent - ?) <= MAX(ABS(p.tp2_percent), ABS(?), 1e-12) * 0.20
      AND ABS(p.tp3_percent - ?) <= MAX(ABS(p.tp3_percent), ABS(?), 1e-12) * 0.20
    )
  )
`;

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

function fixedTimeEquals(left, right) {
  const encoder = new TextEncoder();
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  if (a.length !== b.length || a.length === 0) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index += 1) {
    difference |= a[index] ^ b[index];
  }
  return difference === 0;
}

function authorized(request, env) {
  const expected = String(env.ACCOUNT03_LEDGER_API_TOKEN || "");
  const header = request.headers.get("authorization") || "";
  if (expected.length < 32 || !header.startsWith("Bearer ")) return false;
  return fixedTimeEquals(header.slice(7), expected);
}

async function readJson(request) {
  const declaredLength = Number(request.headers.get("content-length") || "0");
  if (declaredLength > 8192) throw new Error("body_too_large");
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > 8192) throw new Error("body_too_large");
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error("invalid_json");
  }
}

function positiveNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function normalizeReservation(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  if (body.account_id !== "account_03") return null;
  if (typeof body.reservation_id !== "string" || !/^[A-Za-z0-9_-]{16,80}$/.test(body.reservation_id)) return null;

  const raw = body.signal;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const signalId = typeof raw.signal_id === "string" ? raw.signal_id.trim().slice(0, 128) : "";
  const symbol = typeof raw.symbol === "string" ? raw.symbol.trim().toUpperCase() : "";
  const baseAsset = typeof raw.base_asset === "string" ? raw.base_asset.trim().toUpperCase() : "";
  const side = typeof raw.side === "string" ? raw.side.trim().toUpperCase() : "";
  const timeframe = typeof raw.timeframe === "string" ? raw.timeframe.trim().toLowerCase() : "";

  if (!/^[A-Z0-9._-]{2,40}$/.test(symbol)) return null;
  if (!/^[A-Z0-9]{1,20}$/.test(baseAsset)) return null;
  if (side !== "LONG" && side !== "SHORT") return null;
  if (!/^[0-9]{1,3}[mhdw]$/.test(timeframe)) return null;

  const numeric = {
    entry_mid: raw.entry_mid,
    stop_percent: raw.stop_percent,
    tp1_percent: raw.tp1_percent,
    tp2_percent: raw.tp2_percent,
    tp3_percent: raw.tp3_percent,
  };
  if (!Object.values(numeric).every(positiveNumber)) return null;

  return {
    reservation_id: body.reservation_id,
    signal: {
      signal_id: signalId,
      symbol,
      base_asset: baseAsset,
      side,
      timeframe,
      ...numeric,
    },
  };
}

async function exists(db, sql, values = []) {
  const row = await db.prepare(sql).bind(...values).first();
  return row !== null;
}

async function countRows(db, sql, values = []) {
  const row = await db.prepare(sql).bind(...values).first();
  return Number(row?.count || 0);
}

function duplicateValues(signal, cutoffMs) {
  return [
    cutoffMs,
    signal.signal_id,
    signal.signal_id,
    signal.base_asset,
    signal.side,
    signal.timeframe,
    signal.entry_mid,
    signal.entry_mid,
    signal.stop_percent,
    signal.stop_percent,
    signal.tp1_percent,
    signal.tp1_percent,
    signal.tp2_percent,
    signal.tp2_percent,
    signal.tp3_percent,
    signal.tp3_percent,
  ];
}

async function reserve(db, reservation) {
  const signal = reservation.signal;
  const nowMs = Date.now();
  const iso = new Date(nowMs).toISOString();
  const utcDay = iso.slice(0, 10);
  const hourBucket = iso.slice(0, 13);
  const cutoffMs = nowMs - 24 * 60 * 60 * 1000;

  const sql = `
    INSERT OR IGNORE INTO publish_attempts (
      reservation_id, account_id, signal_id, symbol, base_asset,
      side, timeframe, entry_mid, stop_percent, tp1_percent, tp2_percent, tp3_percent,
      created_at_ms, utc_day, hour_bucket, status
    )
    SELECT
      ?, 'account_03', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'publish_pending'
    WHERE NOT EXISTS (
      SELECT 1 FROM publish_attempts
      WHERE account_id = 'account_03' AND hour_bucket = ?
    )
    AND (
      SELECT COUNT(*) FROM publish_attempts
      WHERE account_id = 'account_03' AND utc_day = ?
    ) < 24
    AND (
      SELECT COUNT(*) FROM publish_attempts
      WHERE account_id = 'account_03' AND utc_day = ? AND base_asset = ?
    ) < 2
    AND NOT EXISTS (
      SELECT 1 FROM publish_attempts p WHERE ${DUPLICATE_MATCH}
    )
  `;

  const values = [
    reservation.reservation_id,
    signal.signal_id,
    signal.symbol,
    signal.base_asset,
    signal.side,
    signal.timeframe,
    signal.entry_mid,
    signal.stop_percent,
    signal.tp1_percent,
    signal.tp2_percent,
    signal.tp3_percent,
    nowMs,
    utcDay,
    hourBucket,
    hourBucket,
    utcDay,
    utcDay,
    signal.base_asset,
    ...duplicateValues(signal, cutoffMs),
  ];

  const inserted = await db.prepare(sql).bind(...values).run();
  if (Number(inserted?.meta?.changes || 0) === 1) {
    return { reserved: true, reservation_id: reservation.reservation_id };
  }

  if (await exists(
    db,
    "SELECT 1 AS hit FROM publish_attempts WHERE account_id = 'account_03' AND hour_bucket = ? LIMIT 1",
    [hourBucket],
  )) return { reserved: false, reason: "hourly_limit" };

  if (await countRows(
    db,
    "SELECT COUNT(*) AS count FROM publish_attempts WHERE account_id = 'account_03' AND utc_day = ?",
    [utcDay],
  ) >= 24) return { reserved: false, reason: "account_daily_limit" };

  if (await countRows(
    db,
    "SELECT COUNT(*) AS count FROM publish_attempts WHERE account_id = 'account_03' AND utc_day = ? AND base_asset = ?",
    [utcDay, signal.base_asset],
  ) >= 2) return { reserved: false, reason: "coin_daily_limit" };

  if (await exists(
    db,
    `SELECT 1 AS hit FROM publish_attempts p WHERE ${DUPLICATE_MATCH} LIMIT 1`,
    duplicateValues(signal, cutoffMs),
  )) return { reserved: false, reason: "duplicate_signal_24h" };

  return { reserved: false, reason: "reservation_conflict" };
}

async function finalize(db, reservationId, status) {
  const nowMs = Date.now();
  const update = await db.prepare(`
    UPDATE publish_attempts
    SET status = ?, finalized_at_ms = ?
    WHERE reservation_id = ?
      AND account_id = 'account_03'
      AND status = 'publish_pending'
  `).bind(status, nowMs, reservationId).run();

  if (Number(update?.meta?.changes || 0) === 1) {
    return { finalized: true, status };
  }

  const existing = await db.prepare(`
    SELECT status FROM publish_attempts
    WHERE reservation_id = ? AND account_id = 'account_03'
    LIMIT 1
  `).bind(reservationId).first();

  if (!existing) return { finalized: false, reason: "reservation_not_found" };
  if (existing.status === status) return { finalized: true, status, already_finalized: true };
  return { finalized: false, reason: "reservation_already_finalized", current_status: existing.status };
}

const hex = (buffer) =>
  [...new Uint8Array(buffer)].map((value) => value.toString(16).padStart(2, "0")).join("");

async function minuteForHour(seed, hourKey) {
  if (!seed || seed.length < 16) {
    throw new Error("SLOT_RANDOM_SEED must be at least 16 characters");
  }
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`${seed}:${hourKey}`),
  );
  const bytes = new Uint8Array(digest);
  const value = new DataView(bytes.buffer).getUint32(0, false);
  return value % 60;
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return hex(digest);
}

async function createSlotAuthorization(db, hourKey) {
  if (!db || typeof db.prepare !== "function") {
    throw new Error("Account 03 D1 binding is unavailable");
  }

  const randomBytes = crypto.getRandomValues(new Uint8Array(32));
  const nonce = hex(randomBytes);
  const tokenHash = await sha256Hex(nonce);
  const nowMs = Date.now();
  const expiresAtMs = nowMs + 10 * 60 * 1000;

  // Retain a short audit window while preventing table growth without bound.
  await db.prepare(
    "DELETE FROM slot_authorizations WHERE expires_at_ms < ?"
  ).bind(nowMs - 24 * 60 * 60 * 1000).run();

  const inserted = await db.prepare(`
    INSERT OR IGNORE INTO slot_authorizations (
      token_hash, account_id, hour_bucket, created_at_ms, expires_at_ms
    ) VALUES (?, 'account_03', ?, ?, ?)
  `).bind(tokenHash, hourKey, nowMs, expiresAtMs).run();

  if (Number(inserted?.meta?.changes || 0) !== 1) {
    throw new Error("A scheduler authorization already exists for this UTC hour");
  }
  return nonce;
}

async function consumeSlotAuthorization(db, nonce) {
  if (typeof nonce !== "string" || !/^[0-9a-f]{64}$/.test(nonce)) {
    return { consumed: false, reason: "invalid_expired_or_replayed" };
  }

  const nowMs = Date.now();
  const iso = new Date(nowMs).toISOString();
  const currentHour = `${iso.slice(0, 10)}-${iso.slice(11, 13)}`;
  const tokenHash = await sha256Hex(nonce);
  const result = await db.prepare(`
    UPDATE slot_authorizations
    SET consumed_at_ms = ?
    WHERE token_hash = ?
      AND account_id = 'account_03'
      AND hour_bucket = ?
      AND consumed_at_ms IS NULL
      AND expires_at_ms >= ?
  `).bind(nowMs, tokenHash, currentHour, nowMs).run();

  if (Number(result?.meta?.changes || 0) !== 1) {
    return { consumed: false, reason: "invalid_expired_or_replayed" };
  }
  return { consumed: true, hour_bucket: currentHour };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      if (
        !env.ACCOUNT03_PUBLISH_LEDGER ||
        typeof env.ACCOUNT03_PUBLISH_LEDGER.prepare !== "function" ||
        !env.ACCOUNT03_LEDGER_API_TOKEN ||
        String(env.ACCOUNT03_LEDGER_API_TOKEN).length < 32
      ) {
        return jsonResponse({ ok: false, error: "ledger_not_ready" }, 503);
      }

      try {
        await env.ACCOUNT03_PUBLISH_LEDGER.prepare(
          "SELECT COUNT(*) AS count FROM publish_attempts WHERE account_id = 'account_03'"
        ).first();
        return jsonResponse({ ok: true, service: "account03-publish-ledger" });
      } catch {
        return jsonResponse({ ok: false, error: "ledger_unavailable" }, 503);
      }
    }

    if (!url.pathname.startsWith("/v1/")) {
      return jsonResponse({ error: "not_found" }, 404);
    }
    if (!env.ACCOUNT03_PUBLISH_LEDGER || typeof env.ACCOUNT03_PUBLISH_LEDGER.prepare !== "function") {
      return jsonResponse({ error: "ledger_unavailable" }, 503);
    }
    if (!env.ACCOUNT03_LEDGER_API_TOKEN || String(env.ACCOUNT03_LEDGER_API_TOKEN).length < 32) {
      return jsonResponse({ error: "ledger_auth_not_configured" }, 503);
    }
    if (!authorized(request, env)) {
      return jsonResponse({ error: "unauthorized" }, 401);
    }

    try {
      if (request.method === "POST" && url.pathname === "/v1/slot-authorizations/consume") {
        let body;
        try {
          body = await readJson(request);
        } catch (error) {
          return jsonResponse({ error: error.message }, 400);
        }
        const result = await consumeSlotAuthorization(
          env.ACCOUNT03_PUBLISH_LEDGER,
          body?.nonce,
        );
        if (!result.consumed) return jsonResponse(result, 409);
        return jsonResponse({ ok: true, ...result });
      }

      if (request.method === "POST" && url.pathname === "/v1/reservations") {
        let body;
        try {
          body = await readJson(request);
        } catch (error) {
          return jsonResponse({ error: error.message }, 400);
        }
        const reservation = normalizeReservation(body);
        if (!reservation) return jsonResponse({ error: "invalid_reservation" }, 400);
        const result = await reserve(env.ACCOUNT03_PUBLISH_LEDGER, reservation);
        return jsonResponse(result, result.reserved ? 201 : 409);
      }

      const finalizeMatch = url.pathname.match(/^\/v1\/reservations\/([A-Za-z0-9_-]{16,80})\/finalize$/);
      if (request.method === "POST" && finalizeMatch) {
        let body;
        try {
          body = await readJson(request);
        } catch (error) {
          return jsonResponse({ error: error.message }, 400);
        }
        const status = body?.status;
        if (status !== "published" && status !== "publish_uncertain") {
          return jsonResponse({ error: "invalid_final_status" }, 400);
        }
        const result = await finalize(env.ACCOUNT03_PUBLISH_LEDGER, finalizeMatch[1], status);
        if (result.finalized) return jsonResponse({ ok: true, ...result });
        return jsonResponse(result, result.reason === "reservation_not_found" ? 404 : 409);
      }

      return jsonResponse({ error: "not_found" }, 404);
    } catch (error) {
      console.error("Account 03 ledger API failure", error?.message || "unknown_error");
      return jsonResponse({ error: "ledger_unavailable" }, 503);
    }
  },

  async scheduled(controller, env) {
    const scheduledAt = new Date(controller.scheduledTime);
    if (Number.isNaN(scheduledAt.getTime())) {
      throw new Error("Cloudflare supplied an invalid scheduledTime");
    }

    const hourKey = [
      scheduledAt.getUTCFullYear(),
      String(scheduledAt.getUTCMonth() + 1).padStart(2, "0"),
      String(scheduledAt.getUTCDate()).padStart(2, "0"),
      String(scheduledAt.getUTCHours()).padStart(2, "0"),
    ].join("-");
    const selectedMinute = await minuteForHour(env.SLOT_RANDOM_SEED, hourKey);
    const actualMinute = scheduledAt.getUTCMinutes();

    console.log(JSON.stringify({
      event: "account03_hourly_slot_check",
      hour_utc: hourKey,
      selected_minute_utc: selectedMinute,
      actual_minute_utc: actualMinute,
    }));

    if (actualMinute !== selectedMinute) return;

    const owner = env.GITHUB_OWNER || "institutionalgiants";
    const repo = env.GITHUB_REPO || "coincoach-account-03";
    const workflow = env.GITHUB_WORKFLOW || "account03-live-square-hourly.yml";
    const branch = env.GITHUB_BRANCH || "main";
    const token = env.GITHUB_TOKEN;
    if (!token) throw new Error("Missing GITHUB_TOKEN secret");
    if (!env.ACCOUNT03_PUBLISH_LEDGER || typeof env.ACCOUNT03_PUBLISH_LEDGER.prepare !== "function") {
      throw new Error("Account 03 D1 binding is unavailable");
    }

    // A high-entropy, short-lived nonce is stored only as a SHA-256 hash in D1.
    // The workflow must atomically consume it before it can enter the publish path.
    const slotNonce = await createSlotAuthorization(env.ACCOUNT03_PUBLISH_LEDGER, hourKey);

    const endpoint = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "CoinCoach-Account03-Scheduler",
      },
      body: JSON.stringify({
        ref: branch,
        inputs: {
          account_id: "account_03",
          slot_nonce: slotNonce,
        },
      }),
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`GitHub workflow dispatch failed (${response.status}): ${body.slice(0, 500)}`);
    }

    console.log(JSON.stringify({
      event: "account03_workflow_dispatched",
      hour_utc: hourKey,
      selected_minute_utc: selectedMinute,
      status: response.status,
    }));
  },
};

export { minuteForHour };
