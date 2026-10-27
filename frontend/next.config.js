/** @type {import('next').NextConfig} */
module.exports = {
  // Emits .next/standalone: a minimal server + only the node_modules it needs (used by the Dockerfile).
  output: 'standalone',
  poweredByHeader: false,
};
