import { useEffect, useState } from 'react';
import { getIpos } from '../lib/api';
import { Link } from 'react-router-dom';
import { useDebounce } from 'use-debounce';
import { Search } from 'lucide-react';

export default function IPOList() {
  const [ipos, setIpos] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  const [debouncedSearch] = useDebounce(searchTerm, 300);

  useEffect(() => {
    getIpos().then(data => {
      setIpos(data);
      setLoading(false);
    }).catch(err => {
      console.error(err);
      setLoading(false);
    });
  }, []);

  const filteredIpos = ipos.filter(ipo => 
    ipo.name.toLowerCase().includes(debouncedSearch.toLowerCase()) || 
    (ipo.sector && ipo.sector.toLowerCase().includes(debouncedSearch.toLowerCase()))
  );

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 animate-in fade-in duration-500">
      <div className="flex flex-col md:flex-row md:items-center justify-between mb-6">
        <h1 className="text-3xl font-serif italic text-zinc-200 mb-4 md:mb-0">All IPOs</h1>
        <div className="relative">
          <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
            <Search className="h-4 w-4 text-zinc-500" />
          </div>
          <input
            type="text"
            className="block w-full md:w-64 pl-10 pr-3 py-2 border border-white/10 rounded-lg leading-5 bg-white/5 text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-500/50 sm:text-sm transition-colors"
            placeholder="Search by name or sector..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />
        </div>
      </div>

      {loading ? (
        <div className="flex justify-center items-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-emerald-500"></div>
        </div>
      ) : (
        <div className="bg-[#121214] rounded-2xl border border-white/5 overflow-hidden flex flex-col">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-white/[0.02] text-zinc-500 text-[10px] uppercase tracking-tighter border-b border-white/5">
                <tr className="text-left">
                  <th scope="col" className="px-6 py-4 font-semibold">Company</th>
                  <th scope="col" className="px-6 py-4 font-semibold">Sector</th>
                  <th scope="col" className="px-6 py-4 font-semibold">Listed Date</th>
                  <th scope="col" className="px-6 py-4 font-semibold text-right">Subscription (x)</th>
                  <th scope="col" className="px-6 py-4 font-semibold text-right">Gain (%)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/5">
                {filteredIpos.map((ipo) => (
                  <tr key={ipo.id} className="hover:bg-white/[0.02] transition-colors">
                    <td className="px-6 py-4 whitespace-nowrap font-medium">
                      <Link to={`/ipo/${encodeURIComponent(ipo.name)}`} className="text-zinc-200 hover:text-emerald-400 transition-colors">
                        {ipo.name}
                      </Link>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-zinc-400">
                      {ipo.sector || '—'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-zinc-500 font-mono text-xs">
                      {ipo.listed_date || '—'}
                      {ipo.listing_gain_pct === null && ipo.subscription_total === null && (
                        <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded text-[9px] font-bold uppercase tracking-widest bg-amber-500/10 text-amber-400 border border-amber-500/20">
                          Upcoming
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right font-mono text-zinc-300">
                      {ipo.subscription_total ? ipo.subscription_total.toFixed(2) : '—'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right font-mono font-bold">
                      {ipo.listing_gain_pct !== null ? (
                        <span className={ipo.listing_gain_pct > 0 ? 'text-emerald-400' : ipo.listing_gain_pct < 0 ? 'text-rose-400' : 'text-zinc-400'}>
                          {ipo.listing_gain_pct > 0 ? '+' : ''}{ipo.listing_gain_pct.toFixed(2)}%
                        </span>
                      ) : (
                        <span className="text-xs text-zinc-500 font-sans">N/A</span>
                      )}
                    </td>
                  </tr>
                ))}
                {filteredIpos.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-6 py-10 text-center text-zinc-500">
                      No IPOs found matching "{searchTerm}"
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
