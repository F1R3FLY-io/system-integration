/* eslint-env node */
'use strict'

const { DataPlaneServer, Database, RepoSubscription } = require('@atproto/bsky')

const main = async () => {
  const dbUrl = process.env.BSKY_DB_POSTGRES_URL
  const port = parseInt(process.env.DATAPLANE_PORT || '2585', 10)
  const plcUrl = process.env.BSKY_DID_PLC_URL || 'https://plc.directory'
  const pdsUrl = process.env.DATAPLANE_REPOSUBSCRIPTION_URLS || process.env.BSKY_DATAPLANE_URLS || 'http://pds:2583'
  
  if (!dbUrl) {
    throw new Error('BSKY_DB_POSTGRES_URL is required')
  }

  console.log(`Starting DataPlane server on port ${port}...`)
  console.log(`PDS subscription URL: ${pdsUrl}`)
  
  const db = new Database({
    url: dbUrl,
    schema: process.env.BSKY_DB_POSTGRES_SCHEMA,
  })

  const dataplane = await DataPlaneServer.create(db, port, plcUrl)
  
  console.log(`DataPlane server listening on port ${port}`)

  // Start repo subscription to index data from PDS
  console.log('Starting repo subscription to PDS...')
  const subscription = new RepoSubscription({
    service: pdsUrl,
    db: db,
    idResolver: dataplane.idResolver,
  })
  
  subscription.start()
  console.log('Repo subscription started')

  // Graceful shutdown
  const shutdown = async () => {
    console.log('Shutting down DataPlane server...')
    await subscription.destroy()
    await dataplane.destroy()
    await db.close()
    process.exit(0)
  }

  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}

main().catch((err) => {
  console.error('DataPlane server failed to start:', err)
  process.exit(1)
})

