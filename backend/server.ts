import express from 'express';
import path from 'path';
import { getIpoSummary, getIpos, getIpoByName, predictWeightedGain } from './db';
import { runRefresh, cache, discoverNewIPOs } from './refresh';
import { fetchIPODetailFromChittorgarh } from './fetcher';
import { seedDatabase } from './seed';
import { createServer as createViteServer } from 'vite';

async function startServer() {
  const app = express();
  const PORT = 3000;

  app.use(express.json());

  // Ensure DB is seeded on startup
  try {
    await seedDatabase();
  } catch (err) {
    console.error('Failed to seed DB on startup:', err);
  }

  // API Routes
  app.get('/api/summary', async (req, res) => {
    try {
      const cached = cache.get('summary');
      if (cached) return res.json(cached);

      const summary = await getIpoSummary();
      cache.set('summary', summary);
      res.json(summary);
    } catch (error) {
      res.status(500).json({ error: 'Failed to fetch summary' });
    }
  });

  app.post('/api/discover-ipos', async (req, res) => {
    try {
      await discoverNewIPOs();
      cache.flushAll(); // Flush cache since new data might be added
      res.json({ success: true });
    } catch (error) {
      res.status(500).json({ error: 'Failed to discover IPOs' });
    }
  });

  app.get('/api/fetch-missing', async (req, res) => {
    try {
      const { name } = req.query;
      if (!name || typeof name !== 'string') {
        return res.status(400).json({ error: 'Name query parameter is required' });
      }
      const updatedIpo = await fetchIPODetailFromChittorgarh(name);
      cache.flushAll(); // Flush cache for updated prediction/summary
      res.json(updatedIpo);
    } catch (error) {
      res.status(500).json({ error: 'Failed to fetch missing details' });
    }
  });


  app.get('/api/ipos', async (req, res) => {
    try {
      const ipos = await getIpos();
      res.json(ipos);
    } catch (error) {
      res.status(500).json({ error: 'Failed to fetch IPOs' });
    }
  });

  app.get('/api/ipo-details', async (req, res) => {
    try {
      const { name } = req.query;
      if (!name || typeof name !== 'string') {
        return res.status(400).json({ error: 'Name query parameter is required' });
      }
      const ipo = await getIpoByName(name);
      if (!ipo) return res.status(404).json({ error: 'IPO not found' });
      res.json(ipo);
    } catch (error) {
      res.status(500).json({ error: 'Failed to fetch IPO' });
    }
  });

  app.get('/api/ipo', async (req, res) => {
    try {
      const { name } = req.query;
      if (!name || typeof name !== 'string') {
        return res.status(400).json({ error: 'Name query parameter is required' });
      }
      const ipo = await getIpoByName(name);
      if (!ipo) return res.status(404).json({ error: 'IPO not found' });
      res.json(ipo);
    } catch (error) {
      res.status(500).json({ error: 'Failed to fetch IPO' });
    }
  });

  app.post('/api/predict-weighted', async (req, res) => {
    try {
      const { subscription } = req.body;
      if (typeof subscription !== 'number' || subscription <= 0) {
        return res.status(400).json({ error: 'Valid subscription number is required' });
      }

      const cacheKey = `predict_${subscription}`;
      const cached = cache.get(cacheKey);
      if (cached) return res.json(cached);

      const prediction = await predictWeightedGain(subscription);
      if (!prediction) {
        return res.status(404).json({ error: 'Not enough data to predict' });
      }

      cache.set(cacheKey, prediction);
      res.json(prediction);
    } catch (error) {
      res.status(500).json({ error: 'Failed to generate prediction' });
    }
  });

  app.post('/api/refresh', async (req, res) => {
    try {
      await runRefresh();
      res.json({ success: true, message: 'Data refreshed successfully' });
    } catch (error) {
      res.status(500).json({ error: 'Failed to refresh data' });
    }
  });

  // Vite middleware for development or serve static in production
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Server running on port ${PORT}`);
  });
}

startServer().catch(console.error);
