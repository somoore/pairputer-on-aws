#!/usr/bin/env node
'use strict';

function summarize(values) {
  return { count: values.length, sum: values.reduce((a, b) => a + b, 0), maximum: values.length ? Math.max(...values) : 0 };
}

if (process.argv.includes('--self-test')) {
  const actual = JSON.stringify(summarize([2, 3, 5]));
  const expected = JSON.stringify({ count: 3, sum: 10, maximum: 5 });
  if (actual !== expected) throw new Error(`${actual} != ${expected}`);
  console.log('NODE_FIXTURE_OK');
} else {
  console.log(JSON.stringify(summarize(process.argv.slice(2).map(Number))));
}

module.exports = { summarize };
