package main

import (
	"fmt"
	"log"

	"github.com/turbot/steampipe-plugin-sdk/v5/grpc/proto"
	"go.riyazali.net/sqlite"
)

type Module struct {
	tableName   string
	columns     SQLiteColumns
	tableSchema *proto.TableSchema
	table       sqlite.VirtualTable
}

func NewModule(tableName string, columns SQLiteColumns, tableSchema *proto.TableSchema) *Module {
	return &Module{
		tableName:   tableName,
		columns:     columns,
		tableSchema: tableSchema,
		table:       &PluginTable{name: tableName, tableSchema: tableSchema, connection: pluginAlias},
	}
}

// Connect is called for the eponymous table (select * from aws_account) and
// for declared tables (create virtual table t using aws_account(...)).
//
// A declared table may name a connection:
//
//	create virtual table prod_account using aws_account(connection='prod');
//
// Without one, or for the eponymous table, the table reads the default
// connection.
func (m *Module) Connect(_ *sqlite.Conn, args []string, declare func(string) error) (sqlite.VirtualTable, error) {
	log.Println("[TRACE] Module.Connect start", m.tableName)
	defer log.Println("[TRACE] Module.Connect end", m.tableName)

	connection, err := parseModuleArgs(args)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", m.tableName, err)
	}
	if connection == "" || connection == defaultConnectionName() {
		return m.table, declare(fmt.Sprintf("CREATE TABLE %s(%s)", m.tableName, m.columns.DeclarationString()))
	}

	schema, ok := connections.get(connection)
	if !ok {
		return nil, fmt.Errorf("%s: connection %q is not configured: call steampipe_configure_%s('%s', '<config>') first", m.tableName, connection, pluginAlias, connection)
	}
	// dynamic plugins can have a different set of tables per connection
	tableSchema, ok := schema.GetSchema()[m.tableName]
	if !ok {
		return nil, fmt.Errorf("table %s does not exist in connection %q", m.tableName, connection)
	}
	logConnection("binding table "+m.tableName+" to", connection)
	columns := getSQLiteColumnsFromTableSchema(tableSchema)
	table := &PluginTable{name: m.tableName, tableSchema: tableSchema, connection: connection}
	return table, declare(fmt.Sprintf("CREATE TABLE %s(%s)", m.tableName, columns.DeclarationString()))
}
