import express from 'express';
import cors from 'cors';
import path from 'path'; // 1. Import path module
import { initializeDatabase, db, IPO } from './db';
import { fetchIPODetailFromChittorgarh } from './fetcher';

const app = express();
app.use(cors());
app.use(express.json());

const PORT = process.env.PORT || 10000;
initializeDatabase();

// --- Serve Frontend --- 

// 2. Add this: Define where your React build lives
// ADJUST THIS PATH to match where your frontend package builds!
// Example options: '../frontend/dist', '../client/build', or '../dist' 
// (Look at your package.json "build" script to find the output folder)
const FRONTEND_BUILD_PATH = path.join(__dirname, '../dist'); 

// 3. Serve static files from the build folder
app.use(express.static(FRONTEND_BUILD_PATH));


// --- API Endpoints ---

app.get('/api/fetch-missing', async (req, res) => {
  const { name } = req.query;
  if (!name) return res.status(400).json({ error: 'Missing name parameter' });

  try {
    const stmt = db.prepare('SELECT * FROM ipos WHERE name = ?');
    const ipo = stmt.get(name as string) as IPO | undefined;

    if (!ipo) return res.status(404).json({ error: 'IPO not found' });

    const updatedIpo = await fetchIPODetailFromChittorgarh(ipo);
    const updated = updatedIpo.detailUrl !== ipo.detailUrl && !!updatedIpo.detailUrl;

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

    res.json({ 
      ipo: updatedIpo, 
      updated: updated 
    });
    
  } catch (error) {
    console.error('Error fetching missing data:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

// 4. Add this: Catch-all handler for React Router.
// If no API route matches, serve the React index.html.
app.get('*', (req, res) => {
  res.sendFile(path.join(FRONTEND_BUILD_PATH, 'index.html'));
});

app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
