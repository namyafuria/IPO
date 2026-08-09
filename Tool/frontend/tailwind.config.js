/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0B0E11',
        panel: '#12161B',
        'panel-raised': '#171C22',
        border: '#232830',
        ink: '#ECEDEE',
        muted: '#8B93A1',
        faint: '#565E6A',
        amber: '#D4A73D',
        'amber-dim': '#8A7128',
        gain: '#3FBF7F',
        loss: '#E5484D',
      },
      fontFamily: {
        display: ['"Space Grotesk"', 'sans-serif'],
        body: ['"IBM Plex Sans"', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'monospace'],
      },
    },
  },
  plugins: [],
}
