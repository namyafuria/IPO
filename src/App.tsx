/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Header from './components/Header';
import Dashboard from './pages/Dashboard';
import IPOList from './pages/IPOList';
import IPODetail from './pages/IPODetail';
import Predictor from './pages/Predictor';

export default function App() {
  return (
    <Router>
      <div className="min-h-screen bg-[#09090b] text-zinc-200 font-sans flex flex-col">
        <Header />
        <main className="flex-1">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/ipos" element={<IPOList />} />
            <Route path="/ipo/:name" element={<IPODetail />} />
            <Route path="/predict" element={<Predictor />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}
