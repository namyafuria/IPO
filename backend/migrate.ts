import { getDb } from './db';

export async function migrate() {
  const db = getDb();
  console.log('Running migration...');
  
  // Add new columns if they don't exist
  const columns = db.pragma('table_info(ipos)') as any[];
  const columnNames = columns.map(c => c.name);

  const addColumn = (colName: string, type: string) => {
    if (!columnNames.includes(colName)) {
      console.log(`Adding column ${colName}...`);
      db.prepare(`ALTER TABLE ipos ADD COLUMN ${colName} ${type}`).run();
    }
  };

  addColumn('price_band_low', 'REAL');
  addColumn('price_band_high', 'REAL');
  addColumn('listed_price', 'REAL');
  addColumn('source_url', 'TEXT');
  addColumn('data_fetched_at', 'TEXT');

  console.log('Updating Ardee Industries...');
  db.prepare(`
    UPDATE ipos SET 
      issue_price = 53,
      issue_size_cr = 425.87,
      price_band_low = 50,
      price_band_high = 53,
      sector = 'Manufacturing',
      listed_date = '2026-08-12',
      subscription_total = null,
      data_source = 'Chittorgarh',
      last_updated = datetime('now'),
      data_fetched_at = datetime('now')
    WHERE name LIKE '%Ardee Industries%'
  `).run();

  console.log('Cleaning up fake entries...');
  db.prepare(`DELETE FROM ipos WHERE name IN ('MV Electrosystem', 'Juniper Green Energy')`).run();

  // Clean up any fake entries or mark them as Seeded so they'll be refreshed
  console.log('Migration complete.');
}

if (import.meta.url === `file://${process.argv[1]}`) {
  migrate().catch(console.error);
}
