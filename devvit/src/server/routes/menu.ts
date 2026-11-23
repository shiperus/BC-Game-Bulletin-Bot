import { Hono } from 'hono';
import type { UiResponse } from '@devvit/web/shared';
import { context } from '@devvit/web/server';
import { createPost } from '../core/post';
import { runIngest } from '../core/ingest';

export const menu = new Hono();

menu.post('/post-create', async (c) => {
  try {
    const post = await createPost();

    return c.json<UiResponse>(
      {
        navigateTo: `https://reddit.com/r/${context.subredditName}/comments/${post.id}`,
      },
      200
    );
  } catch (error) {
    console.error(`Error creating post: ${error}`);
    return c.json<UiResponse>(
      {
        showToast: 'Failed to create post',
      },
      400
    );
  }
});

menu.post('/test-ingest', async (c) => {
  try {
    const result = await runIngest();
    const errors = result.errors.length
      ? ` (${result.errors.length} errors: ${result.errors.join('; ')})`
      : '';
    return c.json<UiResponse>(
      { showToast: `Sent ${result.postCount} posts to Discord${errors}` },
      200
    );
  } catch (error) {
    console.error(`Test ingest failed: ${String(error)}`);
    return c.json<UiResponse>(
      { showToast: `Ingest failed: ${String(error)}`.slice(0, 200) },
      200
    );
  }
});

menu.post('/example-form', async (c) => {
  return c.json<UiResponse>(
    {
      showForm: {
        name: 'exampleForm',
        form: {
          title: 'Example form',
          fields: [{ name: 'message', label: 'Message', type: 'string' }],
        },
      },
    },
    200
  );
});
