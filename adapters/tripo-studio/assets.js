import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  CommandExecutionError,
  EmptyResultError,
} from '@jackwener/opencli/errors';
import {
  callStudioRest,
  ensureTripoPage,
  requireBrowserPage,
  resolveRegion,
} from './_shared.mjs';

const PAGE_SIZE = 20;
const MAX_LIMIT = 100;

function parseLimit(raw) {
  const value = raw == null ? 20 : Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > MAX_LIMIT) {
    throw new ArgumentError(`limit must be an integer between 1 and ${MAX_LIMIT}`);
  }
  return value;
}

function parseOffset(raw) {
  const value = raw == null ? 0 : Number(raw);
  if (!Number.isInteger(value) || value < 0) {
    throw new ArgumentError('offset must be a non-negative integer');
  }
  return value;
}

cli({
  site: 'tripo-studio',
  name: 'assets',
  description: '列出 Tripo Studio 个人或收藏资产列表（只读）',
  access: 'read',
  domain: 'studio.tripo3d.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  example: 'opencli --profile YOUR_PROFILE tripo-studio assets --asset-type mine --type all --limit 20 -f json',
  args: [
    {
      name: 'asset-type',
      type: 'string',
      default: 'mine',
      choices: ['mine', 'collected'],
      help: 'Asset ownership category: mine or collected',
    },
    {
      name: 'type',
      type: 'string',
      default: 'all',
      choices: ['all', 'rigged', 'smart_mesh', 'textured', 'untextured'],
      help: 'Asset filter category',
    },
    {
      name: 'offset',
      type: 'int',
      default: 0,
      help: 'Pagination start offset',
    },
    {
      name: 'limit',
      type: 'int',
      default: 20,
      help: 'Maximum items to return (1-100)',
    },
    {
      name: 'region',
      type: 'string',
      default: 'cn',
      choices: ['cn', 'global'],
      help: 'Target Tripo Studio region (cn|global)',
    },
  ],
  columns: [
    'project_id',
    'operator_id',
    'name',
    'status',
    'model_version',
    'is_owner',
    'is_quad',
    'is_rigged',
    'is_pbr',
    'is_segmented',
    'created_at',
  ],
  func: async (page, kwargs) => {
    requireBrowserPage(page, 'tripo-studio/assets');
    const regionCfg = resolveRegion(kwargs.region);
    const limit = parseLimit(kwargs.limit);
    let currentOffset = parseOffset(kwargs.offset);
    const assetType = String(kwargs['asset-type'] || kwargs.assetType || 'mine');
    const modelType = String(kwargs.type || 'all');

    await ensureTripoPage(page, regionCfg);

    const rows = [];
    while (rows.length < limit) {
      const qs = new URLSearchParams({
        asset_type: assetType,
        offset: String(currentOffset),
        size: String(PAGE_SIZE),
        type: modelType,
      });
      const data = await callStudioRest(page, regionCfg, `/v2/studio/assets/v2?${qs.toString()}`);
      const list = data?.projects;
      if (!Array.isArray(list)) {
        throw new CommandExecutionError('Tripo Studio /v2/studio/assets/v2 did not return projects array');
      }
      if (list.length === 0) {
        break;
      }
      for (const item of list) {
        const op = item?.operator || {};
        rows.push({
          project_id: String(item?.id || ''),
          operator_id: String(op.operator_id || ''),
          name: String(item?.name || ''),
          status: String(op.status || ''),
          model_version: String(op.model_version || ''),
          is_owner: Boolean(item?.is_owner),
          is_quad: Boolean(op.is_quad),
          is_rigged: Boolean(op.is_rigged),
          is_pbr: Boolean(op.is_pbr),
          is_segmented: Boolean(op.is_segmented),
          created_at: String(item?.created_at || op.create_time || ''),
        });
        if (rows.length >= limit) {
          break;
        }
      }
      if (list.length < PAGE_SIZE) {
        break;
      }
      currentOffset += PAGE_SIZE;
    }

    if (rows.length === 0) {
      throw new EmptyResultError(
        `tripo-studio/assets (${assetType}/${modelType})`,
        'No assets found for the requested category/offset.'
      );
    }
    return rows;
  },
});
