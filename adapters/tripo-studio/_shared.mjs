import {
  ArgumentError,
  AuthRequiredError,
  CommandExecutionError,
  LoginWallError,
} from '@jackwener/opencli/errors';

export const REGION_MAP = {
  cn: {
    region: 'cn',
    domain: 'studio.tripo3d.com',
    webOrigin: 'https://studio.tripo3d.com',
    restOrigin: 'https://api-cn.tripo3d.com',
    microUrl: 'https://msapi.tripo3d.com/ms/web/api',
  },
  global: {
    region: 'global',
    domain: 'studio.tripo3d.ai',
    webOrigin: 'https://studio.tripo3d.ai',
    restOrigin: 'https://api.tripo3d.ai',
    microUrl: 'https://msapi.tripo3d.ai/ms/web/api',
  },
};

export function resolveRegion(rawRegion) {
  const key = rawRegion == null ? 'cn' : String(rawRegion).trim().toLowerCase();
  if (!Object.hasOwn(REGION_MAP, key)) {
    throw new ArgumentError(
      `Unsupported Tripo Studio region: "${String(rawRegion)}". Expected "cn" or "global".`,
      'Pass --region cn (https://studio.tripo3d.com) or --region global (https://studio.tripo3d.ai).'
    );
  }
  return REGION_MAP[key];
}

export function requireBrowserPage(page, cmdName) {
  if (!page) {
    throw new CommandExecutionError(`Browser session required for ${cmdName}`);
  }
}

export async function ensureTripoPage(page, regionCfg) {
  const probeAuth = async () =>
    page.evaluate(async () => {
      const origin = location.origin;
      const href = location.href;
      const title = document.title || '';
      const nuxt = typeof window.useNuxtApp === 'function' ? window.useNuxtApp() : null;
      if (!nuxt) {
        return {
          origin,
          href,
          ok: false,
          reason: 'no_nuxt',
          title,
          bodyPreview: (document.body?.innerText || '').slice(0, 100),
        };
      }
      let token = '';
      try {
        if (nuxt.$auth?.token?.get) {
          token = (await nuxt.$auth.token.get()) || '';
        }
      } catch {
        token = '';
      }
      const pinia = nuxt.$pinia;
      const userStore = pinia?._s?.get?.('user-store');
      const isLogin = Boolean(userStore?.isLogin);
      if (!token || !isLogin) {
        return { origin, href, ok: false, reason: 'not_logged_in', isLogin, hasToken: Boolean(token), title };
      }
      return { origin, href, ok: true };
    });

  let state = await probeAuth();
  if (state.origin !== regionCfg.webOrigin || !state.ok) {
    if (state.origin !== regionCfg.webOrigin || state.reason === 'no_nuxt') {
      await page.goto(`${regionCfg.webOrigin}/workspace/generate`, {
        waitUntil: 'load',
        settleMs: 1800,
      });
    }
    // Wait up to ~6s for Nuxt CSR/auth hydration (token + user-store.isLogin) after page load
    for (let i = 0; i < 18; i++) {
      state = await probeAuth();
      if (state.origin === regionCfg.webOrigin && state.ok) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 350));
    }
  }

  if (!state.ok) {
    if (state.reason === 'no_nuxt') {
      throw new LoginWallError(
        `Tripo Studio page failed to initialize Nuxt context (title: ${state.title})`,
        403,
        state.href || regionCfg.webOrigin,
        state.bodyPreview || '',
        'Open Chrome (--profile YOUR_PROFILE), verify Tripo Studio loads cleanly, and check WAF/login status.'
      );
    }
    throw new AuthRequiredError(
      regionCfg.domain,
      `Tripo Studio (${regionCfg.domain}) requires an authenticated session in Chrome (--profile YOUR_PROFILE).`
    );
  }
}

export async function callStudioRest(page, regionCfg, endpointPath, opts = {}) {
  const method = opts.method || 'GET';
  const body = opts.body;
  const res = await page.evaluate(
    async ({ restOrigin, endpointPath: ep, method: m, body: b }) => {
      const nuxt = window.useNuxtApp();
      const token = await nuxt.$auth.token.get();
      if (!token) {
        return { ok: false, kind: 'auth', status: 401, url: `${restOrigin}${ep}`, preview: 'missing token' };
      }
      const headers = {
        authorization: `Bearer ${token}`,
      };
      if (nuxt.$tripoLens?.deviceId) {
        headers['x-tripo-device-id'] = String(nuxt.$tripoLens.deviceId);
      }
      const init = {
        method: m,
        credentials: 'omit',
        headers,
      };
      if (b !== undefined) {
        headers['content-type'] = 'application/json';
        init.body = JSON.stringify(b);
      }
      const url = `${restOrigin}${ep}`;
      const resp = await fetch(url, init);
      const text = await resp.text();
      const trimmed = text.trim();
      if (trimmed.startsWith('<!') || trimmed.startsWith('<html')) {
        return { ok: false, kind: 'login_wall', status: resp.status, url, preview: trimmed.slice(0, 100) };
      }
      let json = null;
      try {
        json = JSON.parse(trimmed);
      } catch {
        return { ok: false, kind: 'non_json', status: resp.status, url, preview: trimmed.slice(0, 160) };
      }
      if (!resp.ok) {
        return {
          ok: false,
          kind: resp.status === 401 || resp.status === 403 ? 'auth' : 'http_error',
          status: resp.status,
          url,
          json,
          preview: trimmed.slice(0, 160),
        };
      }
      if (typeof json?.code === 'number' && json.code !== 0) {
        return { ok: false, kind: 'api_error', status: resp.status, url, json };
      }
      return { ok: true, data: json?.data !== undefined ? json.data : json };
    },
    { restOrigin: regionCfg.restOrigin, endpointPath, method, body }
  );

  if (!res.ok) {
    if (res.kind === 'login_wall') {
      throw new LoginWallError(
        `Unexpected HTML response from ${res.url}`,
        res.status,
        res.url,
        res.preview || ''
      );
    }
    if (res.kind === 'auth') {
      throw new AuthRequiredError(
        regionCfg.domain,
        `Tripo Studio API authentication failed (${res.status}) at ${endpointPath}`
      );
    }
    const msg = res.json?.message || res.json?.error || res.preview || `HTTP ${res.status}`;
    const code = res.json?.code != null ? ` (code=${res.json.code})` : '';
    throw new CommandExecutionError(`Tripo Studio REST ${endpointPath} failed: ${msg}${code}`);
  }
  return res.data;
}

export async function callStudioMicro(page, regionCfg, microMethod, body = {}) {
  const res = await page.evaluate(
    async ({ microUrl, microMethod: mm, body: b, region }) => {
      const nuxt = window.useNuxtApp();
      if (region === 'cn' && typeof nuxt.$micro === 'function') {
        try {
          const data = await nuxt.$micro(mm, b);
          return { ok: true, data };
        } catch (err) {
          return {
            ok: false,
            kind: 'micro_error',
            status: err?.status || 500,
            url: microUrl,
            message: err?.message || String(err),
          };
        }
      }
      const token = await nuxt.$auth.token.get();
      if (!token) {
        return { ok: false, kind: 'auth', status: 401, url: microUrl, message: 'missing token' };
      }
      const userStore = nuxt.$pinia?._s?.get?.('user-store');
      const uid = userStore?.detail?.detail?.userId || userStore?.detail?.userId || '';
      const headers = {
        'Content-Type': 'application/json',
        'X-Micro-Method': mm,
        'X-Auth-Token': token,
        'X-Lang': region === 'cn' ? 'zh-CN' : 'en-US',
      };
      if (nuxt.$tripoLens?.deviceId) {
        headers['X-DeviceId'] = String(nuxt.$tripoLens.deviceId);
      }
      if (uid) {
        headers['X-Uid'] = String(uid);
      }
      const resp = await fetch(microUrl, {
        method: 'POST',
        credentials: 'omit',
        headers,
        body: JSON.stringify(b || {}),
      });
      const text = await resp.text();
      const trimmed = text.trim();
      if (trimmed.startsWith('<!') || trimmed.startsWith('<html')) {
        return { ok: false, kind: 'login_wall', status: resp.status, url: microUrl, preview: trimmed.slice(0, 100) };
      }
      try {
        const json = JSON.parse(trimmed);
        if (!resp.ok) {
          return {
            ok: false,
            kind: resp.status === 401 || resp.status === 403 ? 'auth' : 'micro_error',
            status: resp.status,
            url: microUrl,
            message: json?.message || trimmed.slice(0, 160),
          };
        }
        return { ok: true, data: json };
      } catch {
        return { ok: false, kind: 'non_json', status: resp.status, url: microUrl, message: trimmed.slice(0, 160) };
      }
    },
    { microUrl: regionCfg.microUrl, microMethod, body, region: regionCfg.region }
  );

  if (!res.ok) {
    if (res.kind === 'login_wall') {
      throw new LoginWallError(`Unexpected HTML response from ${res.url}`, res.status, res.url, res.preview || '');
    }
    if (res.kind === 'auth') {
      throw new AuthRequiredError(regionCfg.domain, `Tripo Micro API authentication required (${res.status})`);
    }
    throw new CommandExecutionError(`Tripo Studio Micro ${microMethod} failed: ${res.message}`);
  }
  return res.data;
}

export async function readConfigTrials(page) {
  return page.evaluate(() => {
    const nuxt = window.useNuxtApp();
    const cfg = nuxt.$pinia?._s?.get?.('config-store');
    if (!cfg) {
      return null;
    }
    const trialRem = (id) => {
      const t = cfg.trialMap?.[id];
      if (!t || t.is_valid === false) {
        return 0;
      }
      return Math.max(0, Number(t.total_count ?? 0) - Number(t.used_count ?? 0));
    };
    return {
      smart_mesh_p2_trial: Number(cfg.smartMeshP2TrialRemaining ?? 0),
      uv_generate_trial: Number(cfg.uvGenerateTrialRemaining ?? 0),
      ultra_texture_trial: Number(cfg.ultraTextureTrialRemaining ?? 0),
      upscaler_trial: trialRem('texture-upscaler-free-trial'),
      hd_v3_free_trial: trialRem('3.0-hd-free-trial'),
      export_used_count: Number(cfg.exportLimit?.used_count ?? 0),
      export_total_count: Number(cfg.exportLimit?.total_count ?? 0),
    };
  });
}
