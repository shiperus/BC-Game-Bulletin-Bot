import { reddit, settings } from '@devvit/web/server';

export const SOURCE_SUBREDDITS = ['gaming', 'Games', 'GamingLeaksAndRumours'];
const POSTS_PER_SUBREDDIT = 25;
const MAX_BODY_CHARS = 4000;

export type IngestPost = {
  id: string;
  subreddit: string;
  rank: number;
  title: string;
  url: string;
  permalink: string;
  isSelf: boolean;
  // Markdown selftext, truncated; null for link/image posts with no text.
  body: string | null;
  score: number;
  numComments: number;
  flair: string | null;
  createdUtc: number;
  stickied: boolean;
  nsfw: boolean;
};

export type IngestPayload = {
  version: 1;
  fetchedAt: string;
  posts: IngestPost[];
  errors: string[];
};

export type IngestResult = {
  postCount: number;
  errors: string[];
};

async function fetchSubreddit(subredditName: string): Promise<IngestPost[]> {
  const posts = await reddit
    .getHotPosts({
      subredditName,
      limit: POSTS_PER_SUBREDDIT,
      pageSize: POSTS_PER_SUBREDDIT,
    })
    .all();
  return posts.map((post, index) => {
    const permalink = `https://www.reddit.com${post.permalink}`;
    return {
      id: post.id,
      subreddit: subredditName,
      rank: index + 1,
      title: post.title,
      url: post.url,
      permalink,
      // Self-posts point their url back at their own comments page.
      isSelf: post.url.includes(post.permalink),
      // Text-only subs like r/GamingLeaksAndRumours keep their source links here.
      body: post.body ? post.body.slice(0, MAX_BODY_CHARS) : null,
      score: post.score,
      numComments: post.numberOfComments,
      flair: post.flair?.text ?? null,
      createdUtc: Math.floor(post.createdAt.getTime() / 1000),
      stickied: post.stickied,
      nsfw: post.nsfw,
    };
  });
}

export async function collectHotPosts(): Promise<IngestPayload> {
  const posts: IngestPost[] = [];
  const errors: string[] = [];
  for (const subredditName of SOURCE_SUBREDDITS) {
    try {
      posts.push(...(await fetchSubreddit(subredditName)));
    } catch (error) {
      errors.push(`r/${subredditName}: ${String(error)}`);
    }
  }
  return { version: 1, fetchedAt: new Date().toISOString(), posts, errors };
}

async function sendToDiscord(
  webhookUrl: string,
  payload: IngestPayload
): Promise<void> {
  const perSub = SOURCE_SUBREDDITS.map(
    (name) =>
      `r/${name}: ${payload.posts.filter((p) => p.subreddit === name).length}`
  ).join(', ');
  const content = [
    `bc-ingest v${payload.version} ${payload.fetchedAt}`,
    perSub,
    ...payload.errors.map((e) => `error: ${e}`),
  ]
    .join('\n')
    .slice(0, 1900);

  // The full payload rides along as a JSON attachment since ~75 posts won't fit
  // in Discord's 2000-char message limit.
  const form = new FormData();
  form.append(
    'payload_json',
    JSON.stringify({
      content,
      attachments: [{ id: 0, filename: 'ingest.json' }],
    })
  );
  form.append(
    'files[0]',
    new Blob([JSON.stringify(payload)], { type: 'application/json' }),
    'ingest.json'
  );

  const response = await fetch(webhookUrl, { method: 'POST', body: form });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `Discord webhook returned ${response.status}: ${body.slice(0, 300)}`
    );
  }
}

export async function runIngest(): Promise<IngestResult> {
  const webhookUrl = await settings.get<string>('discordWebhookUrl');
  if (!webhookUrl) {
    throw new Error(
      'discordWebhookUrl is not set (run: npx devvit settings set discordWebhookUrl)'
    );
  }
  const payload = await collectHotPosts();
  await sendToDiscord(webhookUrl, payload);
  return { postCount: payload.posts.length, errors: payload.errors };
}
