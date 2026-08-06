import Database from 'better-sqlite3';
import path from 'path';
import fs from 'fs';

export interface IPO {
  id: number;
  name: string;
  symbol: string | null;
  listingDate: string | null;
  price: string | null;
  lotSize: string | null;
  totalIssueSize: string | null;
  detailUrl: string | null;
}

const DB_PATH = path.resolve(__dirname, '../data/ipos.db');
const DB_DIR = path.dirname(DB_PATH);

if (!fs.existsSync(DB_DIR)) {
  fs.mkdirSync(DB_DIR, { recursive: true });
}

export let db: Database.Database;

export function initializeDatabase() {
  db = new Database(DB_PATH);
  db.exec(`
    CREATE TABLE IF NOT EXISTS ipos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL,
      symbol TEXT,
      listingDate TEXT,
      price TEXT,
      lotSize TEXT,
      totalIssueSize TEXT,
      detailUrl TEXT
    )
  `);
  console.log('Database initialized at:', DB_PATH);
  return db;
}
