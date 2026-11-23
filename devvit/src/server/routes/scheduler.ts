import { Hono } from 'hono';
import { runIngest } from '../core/ingest';

export const scheduler = new Hono();

scheduler.post('/ingest', async (c) => {
  try {
    const result = await runIngest();
    console.log(
      `Ingest sent ${result.postCount} posts; errors: ${result.errors.join('; ') || 'none'}`
    );
    return c.json({ status: 'ok' }, 200);
  } catch (error) {
    console.error(`Ingest failed: ${String(error)}`);
    return c.json({ status: 'error' }, 500);
  }
});
