import { cli, Strategy } from '@jackwener/opencli/registry';
import { CommandExecutionError } from '@jackwener/opencli/errors';
import {
  callStudioMicro,
  callStudioRest,
  ensureTripoPage,
  readConfigTrials,
  requireBrowserPage,
  resolveRegion,
} from './_shared.mjs';

function pickNum(obj, keys) {
  if (!obj || typeof obj !== 'object') {
    return 0;
  }
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null) {
      const n = Number(obj[k]);
      if (Number.isFinite(n)) {
        return n;
      }
    }
  }
  return 0;
}

cli({
  site: 'tripo-studio',
  name: 'quota',
  description: '查询 Tripo Studio Smart Mesh P2.0、智能 UV、高清试用与导出限额状态',
  access: 'read',
  domain: 'studio.tripo3d.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  example: 'opencli --profile YOUR_PROFILE tripo-studio quota -f json',
  args: [
    {
      name: 'region',
      type: 'string',
      default: 'cn',
      choices: ['cn', 'global'],
      help: 'Target Tripo Studio region (cn|global)',
    },
  ],
  columns: [
    'region',
    'download_quota_remaining',
    'download_unlimited',
    'smart_mesh_p2_trial',
    'uv_generate_trial',
    'upscaler_trial',
    'hd_v3_free_trial',
    'ultra_texture_trial',
    'op_smart_mesh_v2_remaining',
    'op_uv_edit_remaining',
  ],
  func: async (page, kwargs) => {
    requireBrowserPage(page, 'tripo-studio/quota');
    const regionCfg = resolveRegion(kwargs.region);
    await ensureTripoPage(page, regionCfg);

    const [assetsData, opQuota, trials] = await Promise.all([
      callStudioMicro(page, regionCfg, '/tripo.subscription.api.assets.AssetsApi/GetAssets', {}),
      callStudioRest(page, regionCfg, '/v2/studio/operation/quota', { method: 'POST', body: {} }),
      readConfigTrials(page),
    ]);

    if (!opQuota || typeof opQuota !== 'object') {
      throw new CommandExecutionError('Tripo Studio /v2/studio/operation/quota returned malformed response');
    }

    const quotaRoot = opQuota.quota || opQuota;
    const smartMeshV2 = quotaRoot.generation?.smart_mesh_v2 || {};
    const uvEdit = quotaRoot.uv_edit?.generate || {};
    const dlQuota = assetsData?.downloadQuota || {};

    return [
      {
        region: regionCfg.region,
        download_quota_remaining: Number(dlQuota.availableQuota ?? 0),
        download_unlimited: Boolean(dlQuota.unlimited),
        smart_mesh_p2_trial: trials ? trials.smart_mesh_p2_trial : 0,
        uv_generate_trial: trials ? trials.uv_generate_trial : 0,
        upscaler_trial: trials ? trials.upscaler_trial : 0,
        hd_v3_free_trial: trials ? trials.hd_v3_free_trial : 0,
        ultra_texture_trial: trials ? trials.ultra_texture_trial : 0,
        op_smart_mesh_v2_remaining: pickNum(smartMeshV2, ['remaining', 'remain', 'left', 'available']),
        op_uv_edit_remaining: pickNum(uvEdit, ['remaining', 'remain', 'left', 'available']),
      },
    ];
  },
});
