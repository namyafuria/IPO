import { Link } from 'react-router-dom';
import { TrendingUp, RefreshCw, BarChart2 } from 'lucide-react';
import { useState } from 'react';
import { refreshData } from '../lib/api';

export default function Header() {
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [showSuccess, setShowSuccess] = useState(false);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    try {
      await refreshData();
      setShowSuccess(true);
      setTimeout(() => {
        setShowSuccess(false);
        window.location.reload();
      }, 1000);
    } catch (err) {
      console.error('Failed to refresh', err);
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <header className="h-16 border-b border-white/10 px-4 sm:px-8 flex items-center justify-between bg-[#0c0c0e] sticky top-0 z-10">
      <div className="max-w-7xl mx-auto w-full flex justify-between items-center h-full">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-emerald-500 rounded flex items-center justify-center font-bold text-black">IA</div>
          <Link to="/" className="hover:opacity-80 transition-opacity">
            <h1 className="text-2xl font-serif italic tracking-tight text-zinc-200">IPO <span className="text-emerald-500 font-sans not-italic font-medium uppercase text-xs tracking-widest ml-1">Analyser</span></h1>
          </Link>
        </div>
        <nav className="flex space-x-6 text-sm font-medium text-zinc-400 items-center">
          <Link to="/ipos" className="hover:text-white transition-colors flex items-center space-x-1">
            <BarChart2 className="w-4 h-4" />
            <span className="hidden sm:inline">All IPOs</span>
          </Link>
          <Link to="/predict" className="hover:text-white transition-colors">
            Predictor
          </Link>
          <button 
            onClick={handleRefresh} 
            disabled={isRefreshing}
            className={`flex items-center space-x-1 disabled:opacity-50 transition-colors ${showSuccess ? 'text-emerald-400' : 'hover:text-white'}`}
          >
            <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin' : ''}`} />
            <span className="hidden sm:inline">{showSuccess ? 'Refreshed!' : 'Refresh'}</span>
          </button>
        </nav>
      </div>
    </header>
  );
}
