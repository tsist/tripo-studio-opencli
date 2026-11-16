import { cli, Strategy } from '@jackwener/opencli/registry';
import { CommandExecutionError } from '@jackwener/opencli/errors';
import {
  callStudioMicro,
  ensureTripoPage,
  readConfigTrials,
  requireBrowserPage,
  resolveRegion,
} from './_shared.mjs';

cli({
  site: 'tripo-studio',
  name: 'balance',
  description: '查询 Tripo Studio 网页端会员积分余额、过期积分、下载额度与试用次数',
  access: 'read',
  domain: 'studio.tripo3d.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  example: 'opencli --profile YOUR_PROFILE tripo-studio balance -f json',
  args: [
    {
      name: 'region',
      type: 'string',
      default: 'cn',
      choices: ['cn', 'global'],
      help: 'Target Tripo Studio region (cn=studio.tripo3d.com, global=studio.tripo3d.ai)',
    },
  ],
  columns: [
    'region',
    'credit_balance',
    'expiring_credits',
    'expiring_at',
    'download_quota',
    'membership_level',
    'membership_cycle',
    'membership_end_at',
    'smart_mesh_p2_trial',
    'uv_generate_trial',
    'upscaler_trial',
  ],
  func: async (page, kwargs) => {
    requireBrowserPage(page, 'tripo-studio/balance');
    const regionCfg = resolveRegion(kwargs.region);
    await ensureTripoPage(page, regionCfg);

    const assetsData = await callStudioMicro(
      page,
      regionCfg,
      '/tripo.subscription.api.assets.AssetsApi/GetAssets',
      {}
    );
    if (!assetsData || typeof assetsData !== 'object') {
      throw new CommandExecutionError('Tripo Studio GetAssets returned an empty payload');
    }

    const trials = await readConfigTrials(page);
    const credit = assetsData.credit || {};
    const dlQuota = assetsData.downloadQuota || {};
    const memberships = Array.isArray(assetsData.memberships) ? assetsData.memberships : [];
    const activeMembership =
      memberships.find((m) => String(m?.status || '').toUpperCase().includes('ACTIVE')) ||
      memberships[0] ||
      {};

    const billingCount = activeMembership?.billingPeriod?.count;
    const billingUnit = String(activeMembership?.billingPeriod?.unit || '')
      .replace(/^PERIOD_UNIT_/i, '')
      .toLowerCase();
    const membershipCycle = billingCount && billingUnit ? `${billingCount}_${billingUnit}` : '';

    return [
      {
        region: regionCfg.region,
        credit_balance: Number(credit.availableCredits ?? 0),
        expiring_credits: Number(credit.nextExpiringCredits ?? 0),
        expiring_at: String(credit.nextExpireTime || ''),
        download_quota: dlQuota.unlimited ? -1 : Number(dlQuota.availableQuota ?? 0),
        membership_level: String(activeMembership.level || 'free'),
        membership_cycle: membershipCycle,
        membership_end_at: String(activeMembership.expireTime || ''),
        smart_mesh_p2_trial: trials ? trials.smart_mesh_p2_trial : 0,
        uv_generate_trial: trials ? trials.uv_generate_trial : 0,
        upscaler_trial: trials ? trials.upscaler_trial : 0,
      },
    ];
  },
});
