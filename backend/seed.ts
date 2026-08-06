import { getDb } from './db';

const DATA = [
  {"name": "Adani Wilmar Ltd", "sector": "FMCG/Edible Oil", "listed": "2022-02-08", "sub": 17.37, "gain": 16.3},
  {"name": "Vedant Fashions Ltd (Manyavar)", "sector": "Retail/Apparel", "listed": "2022-02-16", "sub": 2.57, "gain": 8.0},
  {"name": "Campus Activewear Ltd.", "sector": "Footwear / Consumer", "listed": "2022-05-09", "sub": 35.95, "gain": 29.76},
  {"name": "Rainbow Children's Medicare Ltd", "sector": "Healthcare", "listed": "2022-05-10", "sub": 12.43, "gain": -5.9},
  {"name": "Life Insurance Corporation of India", "sector": "Insurance", "listed": "2022-05-17", "sub": 2.05, "gain": -7.75},
  {"name": "Delhivery Ltd", "sector": "Logistics", "listed": "2022-05-24", "sub": 1.33, "gain": 10.32},
  {"name": "Global Health Ltd (Medanta)", "sector": "Healthcare", "listed": "2022-11-16", "sub": 28.46, "gain": 17.0},
  {"name": "Five-Star Business Finance Ltd.", "sector": "NBFC / Financial Services", "listed": "2022-11-21", "sub": 1.01, "gain": 3.38},
  {"name": "Mankind Pharma Ltd", "sector": "Pharmaceuticals", "listed": "2023-05-09", "sub": 11.01, "gain": 31.86},
  {"name": "Utkarsh Small Finance Bank Ltd", "sector": "Banking/NBFC", "listed": "2023-07-21", "sub": 101.91, "gain": 92.0},
  {"name": "Concord Biotech Ltd", "sector": "Pharma/Biotech", "listed": "2023-08-18", "sub": 24.87, "gain": 21.46},
  {"name": "JSW Infrastructure Ltd.", "sector": "Ports / Maritime Infrastructure", "listed": "2023-10-03", "sub": 22.03, "gain": 32.18},
  {"name": "Cello World Ltd.", "sector": "Consumer Houseware / Stationery", "listed": "2023-11-06", "sub": 29.5, "gain": 22.18},
  {"name": "Tata Technologies Ltd", "sector": "IT Enabled Services", "listed": "2023-11-30", "sub": 51.18, "gain": 162.85},
  {"name": "Awfis Space Solutions Ltd", "sector": "Real Estate/Co-working", "listed": "2024-05-30", "sub": 108.56, "gain": 13.58},
  {"name": "Ola Electric Mobility Ltd", "sector": "Automobile/EV", "listed": "2024-08-09", "sub": 4.27, "gain": 20.0},
  {"name": "Premier Energies Ltd", "sector": "Renewable Energy/Solar", "listed": "2024-09-03", "sub": 74.38, "gain": 120.0},
  {"name": "Bajaj Housing Finance Ltd.", "sector": "NBFC / Housing Finance", "listed": "2024-09-16", "sub": 49.63, "gain": 135.71},
  {"name": "Hyundai Motor India Ltd", "sector": "Automobile", "listed": "2024-10-22", "sub": 2.37, "gain": -1.32},
  {"name": "Swiggy Limited", "sector": "E-Retail / E-Commerce (Food Delivery)", "listed": "2024-11-13", "sub": 2.42, "gain": 16.91},
  {"name": "Hexaware Technologies Ltd.", "sector": "IT Services / Software", "listed": "2025-02-19", "sub": 2.25, "gain": 7.7},
  {"name": "Ather Energy Ltd.", "sector": "Automobile (Electric Two-Wheeler)", "listed": "2025-05-06", "sub": 1.26, "gain": -5.83},
  {"name": "HDB Financial Services Ltd.", "sector": "NBFC / Financial Services", "listed": "2025-07-02", "sub": 13.07, "gain": 13.64},
  {"name": "Tata Capital Ltd.", "sector": "NBFC / Financial Services", "listed": "2025-10-13", "sub": 1.65, "gain": 1.38},
  {"name": "Groww (Billionbrains Garage Ventures Ltd)", "sector": "Fintech/Broking", "listed": "2025-11-12", "sub": 17.6, "gain": 12.0},
  {"name": "OnEMI Technology (Kissht)", "sector": null, "listed": "2026-05-08", "sub": 9.42, "gain": 11.11},
  {"name": "Bagmane Prime Office REIT", "sector": null, "listed": "2026-05-14", "sub": 16.36, "gain": 3.5},
  {"name": "CMR Green Technologies", "sector": null, "listed": "2026-06-10", "sub": 28.92, "gain": 39.58},
  {"name": "Hexagon Nutrition", "sector": null, "listed": "2026-06-12", "sub": 52.52, "gain": 7.22},
  {"name": "Turtlemint Fintech Solutions", "sector": null, "listed": "2026-06-29", "sub": 1.17, "gain": -11.25},
  {"name": "Advit Jewels", "sector": null, "listed": "2026-07-01", "sub": 210.57, "gain": 36.88},
  {"name": "Waterways Leisure Tourism", "sector": null, "listed": "2026-07-01", "sub": 1.44, "gain": -15.72},
  {"name": "CSM Technologies", "sector": null, "listed": "2026-07-02", "sub": 1.32, "gain": 0.0},
  {"name": "Aastha Spintex", "sector": null, "listed": "2026-07-06", "sub": 4.6, "gain": -4.41},
  {"name": "Knack Packaging", "sector": null, "listed": "2026-07-08", "sub": 83.04, "gain": 10.59},
  {"name": "Kusumgar", "sector": null, "listed": "2026-07-15", "sub": 127.89, "gain": 35.8},
  {"name": "Laser Power & Infra", "sector": null, "listed": "2026-07-16", "sub": 38.74, "gain": 16.82},
  {"name": "SBI Funds Management", "sector": null, "listed": "2026-07-21", "sub": 41.62, "gain": 6.85},
  {"name": "Alpine Texworld", "sector": null, "listed": "2026-07-21", "sub": 1.38, "gain": 0.0},
  {"name": "Caliber Mining & Logistics", "sector": null, "listed": "2026-07-24", "sub": 146.41, "gain": 17.98},
  {"name": "Xtranet Technologies", "sector": null, "listed": "2026-07-30", "sub": 12.13, "gain": 7.09},
  {"name": "Lohia Corp", "sector": "Industrial Products / Technical Textiles Machinery", "listed": "2026-07-30", "sub": 7.26, "gain": 16.38},
  {"name": "Indo-MIM", "sector": null, "listed": "2026-07-30", "sub": 72.29, "gain": 44.33},
  {"name": "Cube Highways Trust InvIT", "sector": null, "listed": "2026-07-31", "sub": 6.76, "gain": 1.97},
  {"name": "Manipal Health Enterprises", "sector": null, "listed": "2026-08-05", "sub": 4.9, "gain": 10.51}
];

export async function seedDatabase() {
  const db = getDb();
  
  const stmt = db.prepare(`
    INSERT INTO ipos (name, sector, listed_date, subscription_total, listing_gain_pct, data_source, last_updated, gain_basis)
    VALUES (?, ?, ?, ?, ?, 'Seeded', datetime('now'), 'close')
    ON CONFLICT(name) DO UPDATE SET
      sector = excluded.sector,
      listed_date = excluded.listed_date,
      subscription_total = excluded.subscription_total,
      listing_gain_pct = excluded.listing_gain_pct,
      last_updated = excluded.last_updated
  `);

  db.transaction(() => {
    for (const item of DATA) {
      stmt.run(item.name, item.sector, item.listed, item.sub, item.gain);
    }
  })();
  
  console.log('Database seeded successfully.');
}
