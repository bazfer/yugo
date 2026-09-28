// Issue #35: exercise the actual Release-1 writer from a separate checkout.
import { Database } from 'bun:sqlite'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

if (!process.env.RELEASE1_CHECKOUT) throw new Error('RELEASE1_CHECKOUT is required')
const { DurableEnvelopeDedupStore } = await import(
  pathToFileURL(join(process.env.RELEASE1_CHECKOUT, 'src/fleet-bus.ts')).href
)
const directory = mkdtempSync(join(tmpdir(), 'yugo-r1-wide-'))
try {
  const path = join(directory, 'dedup.sqlite')
  const db = new Database(path)
  try {
    db.exec(`CREATE TABLE envelope_dedup_v2 (
      envelope_id TEXT PRIMARY KEY, first_seen_ms INTEGER NOT NULL,
      req_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','completed')),
      lease_owner TEXT NOT NULL, lease_until_ms INTEGER NOT NULL)`)
    db.exec("ALTER TABLE envelope_dedup_v2 ADD COLUMN future TEXT DEFAULT ''")
  } finally { db.close() }
  const store = new DurableEnvelopeDedupStore(path)
  try {
    const claim = store.claim('wide', 'original', 100000)
    if (claim.duplicate || claim.reqId !== 'original' || !claim.owner) {
      throw new Error('claim did not acquire the widened table row')
    }
  } finally { store.db.close() }
} finally { rmSync(directory, { recursive: true, force: true }) }
console.log('PASS: Release-1 claim accepts a seven-column table')
