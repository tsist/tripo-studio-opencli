import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError, CommandExecutionError } from '@jackwener/opencli/errors';
import {
  callStudioRest,
  ensureTripoPage,
  requireBrowserPage,
  resolveRegion,
} from './_shared.mjs';

cli({
  site: 'tripo-studio',
  name: 'project',
  description: '读取指定 Tripo Studio 项目详情与操作历史版本',
  access: 'read',
  domain: 'studio.tripo3d.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  example: 'opencli --profile YOUR_PROFILE tripo-studio project <project_id> -f json',
  args: [
    {
      name: 'id',
      type: 'string',
      required: true,
      positional: true,
      help: 'Project UUID',
    },
    {
      name: 'operator-id',
      type: 'string',
      help: 'Optional operator UUID within project history',
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
    'is_hd_textured',
    'is_ultra_textured',
    'is_segmented',
    'history_count',
    'model_url',
  ],
  func: async (page, kwargs) => {
    requireBrowserPage(page, 'tripo-studio/project');
    const regionCfg = resolveRegion(kwargs.region);
    const projectId = String(kwargs.id || '').trim();
    if (!projectId) {
      throw new ArgumentError('Project id is required');
    }
    const operatorId = kwargs['operator-id'] || kwargs.operatorId;
    await ensureTripoPage(page, regionCfg);

    const detailQs = operatorId ? `?operator_id=${encodeURIComponent(String(operatorId))}` : '';
    const [detail, history] = await Promise.all([
      callStudioRest(page, regionCfg, `/v2/studio/project/detail/v3/${encodeURIComponent(projectId)}${detailQs}`),
      callStudioRest(page, regionCfg, `/v2/studio/project/history/${encodeURIComponent(projectId)}`),
    ]);

    if (!detail || typeof detail !== 'object') {
      throw new CommandExecutionError(`Project detail not found for ${projectId}`);
    }
    const op = detail.operator || {};
    const historyItems = Array.isArray(history)
      ? history
      : Array.isArray(history?.history)
      ? history.history
      : Array.isArray(history?.histories)
      ? history.histories
      : Array.isArray(history?.items)
      ? history.items
      : [];

    return [
      {
        project_id: String(detail.id || projectId),
        operator_id: String(op.operator_id || operatorId || ''),
        name: String(detail.name || ''),
        status: String(op.status || ''),
        model_version: String(op.model_version || ''),
        is_owner: Boolean(detail.is_owner),
        // Current detail/v3 may omit this field even when the viewer reports
        // Quad. Preserve unknown instead of inventing a triangle assertion.
        is_quad: typeof op.is_quad === 'boolean' ? op.is_quad : null,
        is_rigged: Boolean(op.is_rigged),
        is_pbr: Boolean(op.is_pbr),
        is_hd_textured: typeof op.is_hd_textured === 'boolean' ? op.is_hd_textured : null,
        is_ultra_textured: typeof op.is_ultra_textured === 'boolean' ? op.is_ultra_textured : null,
        is_segmented: Boolean(op.is_segmented),
        history_count: historyItems.length,
        model_url: String(detail.model_url || op.model_url || ''),
      },
    ];
  },
});
