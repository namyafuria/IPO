import cron from 'node-cron';
import NodeCache from 'node-cache';
import { getDb } from './db';
import { fetchIPOListFromChittorgarh, fetchIPODetailFromChittorgarh, fetchLiveSubscriptionAndGMP } from './fetcher';

// Initialize a cache for the API (5 minutes TTL)
export const cache = new NodeCache({ stdTTL: 300 });

export async function discoverNewIPOs() {
  const currentYear = new Date().getFullYear();
  await fetchIPOListFromChittorgarh(currentYear);
  await fetchIPOListFromChittorgarh(currentYear - 1);
}

export async function updateLiveIPOs() {
  const db = getDb();
  const activeIpos = db.prepare('SELECT name FROM ipos WHERE listing_gain_pct IS NULL').all() as any[];
  for (const ipo of activeIpos) {
    await fetchLiveSubscriptionAndGMP(ipo.name);
  }
}

export async function fetchMissingDetailsForIPOs() {
  const db = getDb();
  // Find IPOs missing core details or stale data
  const incompleteIpos = db.prepare(`
    SELECT name 
    FROM ipos 
    WHERE issue_price IS NULL 
       OR issue_size_cr IS NULL
       OR pe_ratio IS NULL
       OR data_fetched_at IS NULL
       OR data_fetched_at < datetime('now', '-7 days')
  `).all() as any[];
  
  for (const ipo of incompleteIpos) {
    await fetchIPODetailFromChittorgarh(ipo.name);
    // Simple delay to avoid hammering
    await new Promise(res => setTimeout(res, 500));
  }
}

export async function runRefresh() {
  console.log('Starting full data refresh...');
  
  await discoverNewIPOs();
  await fetchMissingDetailsForIPOs();
  await updateLiveIPOs();
  
  // Flush the cache so new data is returned immediately
  cache.flushAll();
  console.log('Data refresh complete and cache flushed.');
}

// Schedule the cron job for discovery (Daily at 2 AM)
cron.schedule('0 2 * * *', () => {
  console.log('Running daily discovery cron job...');
  discoverNewIPOs()
    .then(() => fetchMissingDetailsForIPOs())
    .then(() => cache.flushAll())
    .catch(console.error);
});

// Schedule the cron job for live updates (Every 6 hours)
cron.schedule('0 */6 * * *', () => {
  console.log('Running 6-hour live updates cron job...');
  updateLiveIPOs().then(() => cache.flushAll()).catch(console.error);
});

