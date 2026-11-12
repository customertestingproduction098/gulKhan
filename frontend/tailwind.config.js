/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        paper: {
          DEFAULT: '#E2E6DD',
          deep: '#D5DACF',
        },
        sheet: {
          DEFAULT: '#F6F7F1',
          deep: '#EBEEE4',
        },
        ink: {
          DEFAULT: '#191B16',
          soft: '#525A4D',
          faint: '#5F6659',
        },
        rule: {
          DEFAULT: '#C2C9BB',
          soft: '#D8DDD1',
        },
        rust: {
          DEFAULT: '#B23B24',
          deep: '#8E2C1A',
        },
        sea: '#1F5D4A',
        ochre: '#855A0C',
      },
      fontFamily: {
        display: ['"Big Shoulders Display"', 'Impact', 'sans-serif'],
        sans: ['Archivo', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"Space Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      borderRadius: {
        sheet: '2px',
      },
      boxShadow: {
        sheet: '0 1px 0 rgba(25, 27, 22, 0.04), 0 18px 32px -30px rgba(25, 27, 22, 0.55)',
        lift: '0 10px 24px -12px rgba(25, 27, 22, 0.35)',
      },
      letterSpacing: {
        signage: '-0.01em',
        label: '0.18em',
      },
    },
  },
  plugins: [],
}
