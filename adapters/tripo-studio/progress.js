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

cli({
  site: 'tripo-studio',
  name: 'progress',
  description: '轮询一个或多个 Tripo Studio operator_id 的实时生成/后处理/导出进度',
  access: 'read',
  domain: 'studio.tripo3d.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  example: 'opencli --profile YOUR_PROFILE tripo-studio progress --ids <op_id> -f json',
  args: [
    {
      name: 'ids',
      type: 'string',
      required: true,
      help: 'Comma-separated operator_id values',
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
    'operator_id',
    'status',
    'progress',
    'task_type',
    'model_url',
    'rendered_image',
    'error_code',
    'error_message',
  ],
  func: async (page, kwargs) => {
    requireBrowserPage(page, 'tripo-studio/progress');
    const regionCfg = resolveRegion(kwargs.region);
    const ids = String(kwargs.ids || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    if (ids.length === 0) {
      throw new ArgumentError('At least one operator_id must be passed via --ids');
    }

    await ensureTripoPage(page, regionCfg);
    const data = await callStudioRest(page, regionCfg, '/v2/studio/progress', {
      method: 'POST',
      body: { ids },
    });

    const list = Array.isArray(data)
      ? data
      : Array.isArray(data?.tasks)
      ? data.tasks
      : Array.isArray(data?.operators)
      ? data.operators
      : data && typeof data === 'object'
      ? Object.values(data)
      : [];

    if (!Array.isArray(list) || list.length === 0) {
      throw new EmptyResultError(`tripo-studio/progress (${ids.join(',')})`, 'No progress records returned.');
    }

    const rows = [];
    for (const item of list) {
      if (!item || typeof item !== 'object') {
        throw new CommandExecutionError('Malformed progress record in /v2/studio/progress response');
      }
      rows.push({
        operator_id: String(item.operator_id || item.id || ''),
        status: String(item.status || ''),
        progress: Number(item.progress ?? 0),
        task_type: String(item.type || item.task_type || ''),
        model_url: String(item.output?.model || item.model_url || ''),
        rendered_image: String(item.output?.rendered_image || item.rendered_image || ''),
        error_code: String(item.error_code ?? ''),
        error_message: String(item.error_msg || item.error_message || ''),
      });
    }
    return rows;
  },
});
