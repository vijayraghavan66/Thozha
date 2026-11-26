declare module 'sql.js' {
  export interface SqlJsStatic {
    Database: typeof Database;
  }

  export class Database {
    constructor();
    constructor(data: ArrayLike<number> | Buffer | null);
    run(sql: string, params?: any[]): Database;
    exec(sql: string, params?: any[]): QueryExecutionResult[];
    prepare(sql: string, params?: any[]): Statement;
    export(): Uint8Array;
    close(): void;
  }

  export class Statement {
    bind(params?: any[]): boolean;
    step(): boolean;
    getAsObject(params?: any[]): Record<string, any>;
    getArray(params?: any[]): any[];
    reset(): void;
    free(): boolean;
  }

  export interface QueryExecutionResult {
    columns: string[];
    values: any[][];
  }

  export function initSqlJs(options?: {
    locateFile?: (file: string) => string;
  }): Promise<SqlJsStatic>;

  export default initSqlJs;
}
