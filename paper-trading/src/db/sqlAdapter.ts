import { initSqlJs, Database as SqlDatabase } from 'sql.js';

// ============================================================================
// SQL.js Database Adapter
// Replaces better-sqlite3 for better Node.js compatibility
// ============================================================================

export class Database {
  private db: SqlDatabase;
  private statements = new Map<string, Statement>();

  constructor(sqlDb: SqlDatabase) {
    this.db = sqlDb;
  }

  static async create(): Promise<Database> {
    const SQL = await initSqlJs();
    const db = new SQL.Database();
    return new Database(db);
  }

  exec(sql: string): void {
    this.db.run(sql);
  }

  prepare(sql: string): Statement {
    if (this.statements.has(sql)) {
      return this.statements.get(sql)!;
    }
    const stmt = new Statement(this.db, sql);
    this.statements.set(sql, stmt);
    return stmt;
  }

  close(): void {
    this.db.close();
  }
}

export class Statement {
  private stmt: any;

  constructor(private db: SqlDatabase, private sql: string) {
    this.stmt = db.prepare(sql);
  }

  run(...params: any[]): void {
    this.stmt.bind(params);
    this.stmt.step();
    this.stmt.reset();
  }

  get(...params: any[]): any {
    this.stmt.bind(params);
    this.stmt.step();
    const row = this.stmt.getAsObject();
    this.stmt.reset();
    return row;
  }

  all(...params: any[]): any[] {
    this.stmt.bind(params);
    const results: any[] = [];
    while (this.stmt.step()) {
      results.push(this.stmt.getAsObject());
    }
    this.stmt.reset();
    return results;
  }

  free(): boolean {
    return this.stmt.free();
  }
}

// Mock for testing compatibility
export default Database;
