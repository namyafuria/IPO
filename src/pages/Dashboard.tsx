import { useEffect, useState } from 'react';
import { getSummary } from '../lib/api';
import { Activity, TrendingUp, Calendar, Hash, Globe, Check } from 'lucide-react';
import { Link } from 'react-router-dom';
import axios from 'axios';

export default function Dashboard() {
  const [summary, setSummary] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [discovering, setDiscovering] = useState(false);
  const [discoverSuccess, setDiscoverSuccess] = useState(false);

  const fetchSummary = () => {
    getSummary().then(data => {
      setSummary(data);
      setLoading(false);
    }).catch(err => {
      console.error(err);
      setLoading(false);
    });
  };

  useEffect(() => {
    fetchSummary();
  }, []);

  const handleDiscover = async () => {
    setDiscovering(true);
    try {
      await axios.post('/api/discover-ipos');
      setDiscoverSuccess(true);
      fetchSummary(); // reload summary
      setTimeout(() => setDiscoverSuccess(false), 3000);
    } catch (err) {
      console.error(err);
    } finally {
      setDiscovering(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-emerald-500"></div>
      </div>
    );
  }

  if (!summary) {
    return <div className="text-center text-rose-500 mt-10">Failed to load data</div>;
  }

  const statCards = [
    { name: 'Total IPOs Tracked', value: summary.total },
    { name: 'Positive Listing Rate', value: `${summary.positiveRate.toFixed(1)}%` },
    { name: 'Median Listing Gain', value: `${summary.medianGain.toFixed(2)}%` },
    { name: 'Years Covered', value: summary.yearsCovered },
  ];

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 animate-in fade-in duration-500">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-8 gap-4">
        <div>
          <h1 className="text-3xl font-serif italic text-zinc-200 mb-2">Market Overview</h1>
          <p className="text-zinc-400 text-sm">Analyse Indian mainboard IPO performance based on subscription data.</p>
        </div>
        <button
          onClick={handleDiscover}
          disabled={discovering || discoverSuccess}
          className="flex items-center gap-2 bg-white/5 hover:bg-white/10 border border-white/10 text-zinc-300 text-xs font-bold uppercase tracking-widest px-4 py-2 rounded-lg transition-colors disabled:opacity-50"
        >
          {discovering ? (
            <div className="animate-spin rounded-full h-3 w-3 border-b-2 border-emerald-500"></div>
          ) : discoverSuccess ? (
            <Check className="w-3 h-3 text-emerald-500" />
          ) : (
            <Globe className="w-3 h-3" />
          )}
          {discovering ? 'Discovering...' : discoverSuccess ? 'Discovered!' : 'Discover New IPOs'}
        </button>
      </div>
      
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 mb-12">
        {statCards.map((stat, idx) => (
          <div key={idx} className="bg-[#18181b] p-5 rounded-xl border border-white/5 flex flex-col justify-between shadow-lg shadow-black/20 h-32">
            <span className="text-xs uppercase tracking-widest text-zinc-500 font-semibold">{stat.name}</span>
            <div className="flex items-baseline gap-2 mt-4">
              <span className="text-3xl font-serif italic text-zinc-200">{stat.value}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        <div className="bg-[#121214] rounded-2xl border border-white/5 p-8 flex flex-col">
          <h2 className="text-lg font-serif italic mb-4 text-zinc-200">Analyse an IPO</h2>
          <p className="text-zinc-400 mb-6 leading-relaxed text-sm flex-1">
            Wondering how much listing gain to expect? Use our weighted prediction engine based on historical subscription multiples.
          </p>
          <Link to="/predict" className="inline-flex items-center justify-center px-6 py-3 text-xs font-bold uppercase tracking-widest rounded-lg text-black bg-emerald-500 hover:bg-emerald-400 transition-colors self-start">
            Try the Predictor
          </Link>
        </div>
        
        <div className="bg-[#121214] rounded-2xl border border-white/5 p-8 flex flex-col">
          <h2 className="text-lg font-serif italic mb-4 text-zinc-200">Historical Data</h2>
          <p className="text-zinc-400 mb-6 leading-relaxed text-sm flex-1">
            Browse our database of recently listed mainboard IPOs, their subscription numbers, and listing day performance.
          </p>
          <Link to="/ipos" className="inline-flex items-center justify-center px-6 py-3 text-xs font-bold uppercase tracking-widest rounded-lg text-zinc-200 bg-white/5 border border-white/10 hover:bg-white/10 transition-colors self-start">
            View All IPOs
          </Link>
        </div>
      </div>
    </div>
  );
}
