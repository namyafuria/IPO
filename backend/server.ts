import express from 'express';
import cors from 'cors';
import { initializeDatabase, db, IPO } from './db';
import { fetchIPODetailFromChittorgarh } from './fetcher';

const app = express();
app.use(cors());
app.use(express.json());

const PORT = process.env.PORT || 10000;
initializeDatabase();

// --- API Endpoints ---

// Existing endpoints remain same...

app.get('/api/fetch-missing', async (req, res) => {
  const { name } = req.query;
  if (!name) return res.status(400).json({ error: 'Missing name parameter' });

  try {
    // 1. Get the current state from DB
    const stmt = db.prepare('SELECT * FROM ipos WHERE name = ?');
    const ipo = stmt.get(name as string) as IPO | undefined;

    if (!ipo) return res.status(404).json({ error: 'IPO not found' });

    // 2. Attempt to fetch fresh details
    const updatedIpo = await fetchIPODetailFromChittorgarh(ipo);

    // 3. Determine if we actually updated anything
    // If "detailUrl" is still empty, or is exactly what it was, update didn't happen
    const updated = updatedIpo.detailUrl !== ipo.detailUrl && !!updatedIpo.detailUrl;

    // 4. Update the DB if updated
    if (updated) {
      const updateStmt = db.prepare(`
        UPDATE ipos SET 
          listingDate = ?, 
          price = ?, 
          lotSize = ?, 
          totalIssueSize = ?, 
          detailUrl = ?
        WHERE id = ?
      `);
      updateStmt.run(
        updatedIpo.listingDate || null,
        updatedIpo.price || null,
        updatedIpo.lotSize || null,
        updatedIpo.totalIssueSize || null,
        updatedIpo.detailUrl || null,
        updatedIpo.id
      );
    }

    // ✅ Correct response object for frontend
    res.json({ 
      ipo: updatedIpo, 
      updated: updated 
    });
    
  } catch (error) {
    console.error('Error fetching missing data:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
