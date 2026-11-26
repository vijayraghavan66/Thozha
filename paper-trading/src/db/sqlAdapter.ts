import DatabaseConstructor from 'better-sqlite3';
import fs from 'fs';
import path from 'path';

// ============================================================================
// SQLite Database Adapter
// Uses better-sqlite3 for synchronous, crash-safe, transactional access
// ============================================================================

export interface Statement {
  run(...params: any[]): { changes: number; lastInsertRowid: number | bigint };
  get(...params: any[]): any;
  all(...params: any[]): any[];
}

export class Database {
  private db: DatabaseConstructor.Database;
  private statements = new Map<string, Statement>();
  private filePath: string;

  constructor(sqlDb: DatabaseConstructor.Database, filePath: string) {
    this.db = sqlDb;
    this.filePath = filePath;
  }

  /**
   * Create or open a database file with crash-safe settings
   */
  static create(filePath: string): Database {
    const dir = path.dirname(filePath);
    if (dir && dir !== '.' && !fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }

    const db = new DatabaseConstructor(filePath, {
      // Crash-safe settings
      verbose: process.env.NODE_ENV === 'development' ? console.log : undefined,
    });

    // Enable WAL mode for better concurrency and crash recovery
    db.pragma('journal_mode = WAL');
    // Synchronous commits for durability
    db.pragma('synchronous = NORMAL');
    // Foreign key enforcement
    db.pragma('foreign_keys = ON');
    // Busy timeout for lock contention
    db.pragma('busy_timeout = 5000');

    return new Database(db, filePath);
  }

  /**
   * Create an in-memory database (for testing)
   */
  static createMemory(): Database {
    const db = new DatabaseConstructor(':memory:');
    db.pragma('foreign_keys = ON');
    return new Database(db, ':memory:');
  }

  /**
   * Check if this is a file-backed database
   */
  get isFileBacked(): boolean {
    return this.filePath !== ':memory:';
  }

  /**
   * Get the database file path
   */
  get path(): string {
    return this.filePath;
  }

  /**
   * Execute SQL without prepared statements
   */
  exec(sql: string): void {
    this.db.exec(sql);
  }

  /**
   * Prepare and cache a statement
   */
  prepare(sql: string): Statement {
    if (this.statements.has(sql)) {
      return this.statements.get(sql)!;
    }
    const stmt = this.db.prepare(sql);
    this.statements.set(sql, stmt);
    return stmt;
  }

  /**
   * Execute a transaction atomically
   */
  transaction<T>(fn: () => T): T {
    return this.db.transaction(fn)();
  }

  /**
   * Checkpoint WAL to main database file
   */
  checkpoint(): void {
    if (this.isFileBacked) {
      this.db.pragma('wal_checkpoint(TRUNCATE)');
    }
  }

  /**
   * Close the database and checkpoint if file-backed
   */
  close(): void {
    if (this.isFileBacked) {
      this.checkpoint();
    }
    this.db.close();
  }
}

export default Database;
