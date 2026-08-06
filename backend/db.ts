import Database from 'better-sqlite3';
import path from 'path';
import fs from 'fs';

let dbInstance: Database.Database | null = null;

export function getDb(): Database.Database {
  if (dbInstance) return dbInstance;
  
  const dbDir = process.env.DB_DIR
    ? path.resolve(process.env.DB_DIR)
    : path.join(process.cwd(), 'backend', 'data');
  if (!fs.existsSync(dbDir)) {
    fs.mkdirSync(dbDir, { recursive: true });
  }

  dbInstance = new Database(path.join(dbDir, 'database.sqlite'));

  // Enable WAL mode for concurrency
  dbInstance.pragma('journal_mode = WAL');

  // Create table if not exists
  dbInstance.exec(`
    CREATE TABLE IF NOT EXISTS ipos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE,
      sector TEXT,
      listed_date TEXT,
      subscription_total REAL,
      listing_gain_pct REAL,
      issue_price REAL,
      issue_size_cr REAL,
      pe_ratio REAL,
      roe REAL,
      debt_equity REAL,
      qib_sub REAL,
      hni_sub REAL,
      rii_sub REAL,
      anchor_pct REAL,
      gmp_percent REAL,
      data_source TEXT,
      last_updated TEXT,
      gain_basis TEXT,
      price_band_low REAL,
      price_band_high REAL,
      listed_price REAL,
      source_url TEXT,
      data_fetched_at TEXT
    );
  `);

  dbInstance.exec(`
    CREATE INDEX IF NOT EXISTS idx_name ON ipos(name);
    CREATE INDEX IF NOT EXISTS idx_subscription_total ON ipos(subscription_total);
    CREATE INDEX IF NOT EXISTS idx_listed_date ON ipos(listed_date);
  `);

  return dbInstance;
}

export async function getIpoSummary() {
  const db = getDb();
  
  const total = db.prepare('SELECT COUNT(*) as count FROM ipos').get() as {count: number};
  const positive = db.prepare('SELECT COUNT(*) as count FROM ipos WHERE listing_gain_pct > 0').get() as {count: number};
  
  const allGains = db.prepare('SELECT listing_gain_pct FROM ipos WHERE listing_gain_pct IS NOT NULL ORDER BY listing_gain_pct ASC').all() as {listing_gain_pct: number}[];
  let medianGain = 0;
  if (allGains.length > 0) {
    const mid = Math.floor(allGains.length / 2);
    if (allGains.length % 2 === 0) {
      medianGain = (allGains[mid - 1].listing_gain_pct + allGains[mid].listing_gain_pct) / 2;
    } else {
      medianGain = allGains[mid].listing_gain_pct;
    }
  }
  
  const minDate = db.prepare('SELECT MIN(listed_date) as date FROM ipos').get() as {date: string};
  const maxDate = db.prepare('SELECT MAX(listed_date) as date FROM ipos').get() as {date: string};
  
  const minYear = minDate?.date ? new Date(minDate.date).getFullYear() : new Date().getFullYear();
  const maxYear = maxDate?.date ? new Date(maxDate.date).getFullYear() : new Date().getFullYear();

  return {
    total: total?.count || 0,
    positiveRate: total?.count ? ((positive?.count || 0) / total.count) * 100 : 0,
    medianGain,
    yearsCovered: `${minYear}-${maxYear}`
  };
}

export async function getIpos() {
  const db = getDb();
  return db.prepare('SELECT * FROM ipos ORDER BY listed_date DESC').all();
}

export async function getIpoByName(name: string) {
  const db = getDb();
  return db.prepare('SELECT * FROM ipos WHERE name LIKE ?').get(`%${name}%`);
}

export async function predictWeightedGain(subscription: number) {
  const db = getDb();
  const ipos = db.prepare('SELECT name, subscription_total, listing_gain_pct FROM ipos WHERE listing_gain_pct IS NOT NULL AND subscription_total IS NOT NULL').all() as any[];
  
  if (ipos.length === 0) return null;

  const bandwidth = 0.5; 
  let totalWeight = 0;
  let weightedGainSum = 0;
  
  const similarIpos = ipos.map(ipo => {
    // Distance in log space since subscription is multiplicative
    const dist = Math.log(ipo.subscription_total) - Math.log(subscription);
    // Gaussian kernel
    const weight = Math.exp(-(dist * dist) / (2 * bandwidth * bandwidth));
    return { ...ipo, weight };
  }).sort((a, b) => b.weight - a.weight);

  // Consider top similar IPOs (e.g. top 20 or all with weight > 0.1)
  const topSimilar = similarIpos.slice(0, 20);

  let positiveCount = 0;
  const gains = [];

  for (const ipo of topSimilar) {
    totalWeight += ipo.weight;
    weightedGainSum += ipo.listing_gain_pct * ipo.weight;
    gains.push(ipo.listing_gain_pct);
    if (ipo.listing_gain_pct > 0) positiveCount++;
  }

  const weightedGain = totalWeight > 0 ? weightedGainSum / totalWeight : 0;
  gains.sort((a, b) => a - b);
  
  let median = 0;
  if (gains.length > 0) {
    const mid = Math.floor(gains.length / 2);
    median = gains.length % 2 === 0 ? (gains[mid - 1] + gains[mid]) / 2 : gains[mid];
  }

  return {
    weightedGain,
    median,
    min: gains[0] || 0,
    max: gains[gains.length - 1] || 0,
    positiveRate: topSimilar.length > 0 ? (positiveCount / topSimilar.length) * 100 : 0,
    count: topSimilar.length,
    samples: topSimilar.map(i => ({ name: i.name, sub: i.subscription_total, gain: i.listing_gain_pct, weight: i.weight }))
  };
}
