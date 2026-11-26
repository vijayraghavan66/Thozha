import initSqlJs, { Database as SqlDatabase } from 'sql.js';
import fs from 'fs';
import path from 'path';

// ============================================================================
// SQL.js Database Adapter
// Provides a better-sqlite3-compatible API using sql.js for in-memory/file DB
// ============================================================================

export interface Statement {
  run(...params: any[]): void;
  get(...params: any[]): any;
  all(...params: any[]): any[];
}

export class Database {
  private db: SqlDatabase;
  private statements = new Map<string, Statement>();
  private filePath: string | null = null;

  constructor(sqlDb: SqlDatabase, filePath: string | null = null) {
    this.db = sqlDb;
    this.filePath = filePath;
  }

  static async create(filePath?: string): Promise<Database> {
    const SQL = await initSqlJs();
    
    if (filePath && fs.existsSync(filePath)) {
      // Load existing database from file
      const buffer = fs.readFileSync(filePath);
      const db = new SQL.Database(buffer);
      return new Database(db, filePath);
    } else {
      // Create in-memory database
      const db = new SQL.Database();
      return new Database(db, filePath || null);
    }
  }

  async save(): Promise<void> {
    if (this.filePath) {
      const data = this.db.export();
      const buffer = Buffer.from(data);
      fs.writeFileSync(this.filePath, buffer);
    }
  }

  exec(sql: string): void {
    this.db.run(sql);
  }

  prepare(sql: string): Statement {
    if (this.statements.has(sql)) {
      return this.statements.get(sql)!;
    }
    const stmt = new SqlStatement(this.db, sql);
    this.statements.set(sql, stmt);
    return stmt;
  }

  close(): void {
    this.db.close();
  }
}

class SqlStatement implements Statement {
  private stmt: any;

  constructor(private db: SqlDatabase, private sql: string) {
    this.stmt = db.prepare(sql);
  }

  run(...params: any[]): void {
    const cleanParams = params.map((p) => (p === undefined ? null : p));
    this.stmt.bind(cleanParams);
    this.stmt.step();
    this.stmt.reset();
  }

  get(...params: any[]): any {
    const cleanParams = params.map((p) => (p === undefined ? null : p));
    this.stmt.bind(cleanParams);
    const hasRow = this.stmt.step();
    if (!hasRow) {
      this.stmt.reset();
      return undefined;
    }
    const row = this.stmt.getAsObject();
    this.stmt.reset();
    // sql.js returns {} for empty results on getAsObject
    if (Object.keys(row).length === 0) return undefined;
    return row;
  }

  all(...params: any[]): any[] {
    const cleanParams = params.map((p) => (p === undefined ? null : p));
    this.stmt.bind(cleanParams);
    const results: any[] = [];
    while (this.stmt.step()) {
      results.push(this.stmt.getAsObject());
    }
    this.stmt.reset();
    return results;
  }
}

export default Database;
