/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "../templates/**/*.html",
    "../static/*.js",
  ],
  theme: {
    extend: {
      fontFamily: { mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'] },
      keyframes: {
        fadeUp:  { '0%': { opacity: 0, transform: 'translateY(14px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
        fadeIn:  { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
        scaleIn: { '0%': { opacity: 0, transform: 'scale(.96)' }, '100%': { opacity: 1, transform: 'scale(1)' } },
        slideIn: { '0%': { opacity: 0, transform: 'translateX(-12px)' }, '100%': { opacity: 1, transform: 'translateX(0)' } },
        shimmer: { '0%': { backgroundPosition: '-200% 0' }, '100%': { backgroundPosition: '200% 0' } },
        floaty:  { '0%,100%': { transform: 'translateY(0)' }, '50%': { transform: 'translateY(-6px)' } },
        drift:   { '0%,100%': { backgroundPosition: '0% 50%' }, '50%': { backgroundPosition: '100% 50%' } },
      },
      animation: {
        fadeUp:  'fadeUp .5s cubic-bezier(.22,1,.36,1) both',
        fadeIn:  'fadeIn .45s ease both',
        scaleIn: 'scaleIn .45s cubic-bezier(.22,1,.36,1) both',
        slideIn: 'slideIn .45s cubic-bezier(.22,1,.36,1) both',
        shimmer: 'shimmer 2.2s linear infinite',
        floaty:  'floaty 7s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
