package main

import (
	"errors"
	"fmt"
	"log"

	"github.com/turbot/steampipe-plugin-sdk/v6/grpc/proto"
	"go.riyazali.net/sqlite"
)

// ConfigureFn implements a custom scalar sql function
// that allows the user to configure the plugin connection
type ConfigureFn struct {
	api *sqlite.ExtensionApi
}

func NewConfigureFn(api *sqlite.ExtensionApi) *ConfigureFn {
	return &ConfigureFn{
		api: api,
	}
}

// Args is -1: the function takes either (config) for the default connection
// or (connection_name, config) for a named connection.
func (m *ConfigureFn) Args() int           { return -1 }
func (m *ConfigureFn) Deterministic() bool { return true }
func (m *ConfigureFn) Apply(ctx *sqlite.Context, values ...sqlite.Value) {
	log.Println("[TRACE] ConfigureFn.Apply start")
	defer log.Println("[TRACE] ConfigureFn.Apply end")

	var name, config string
	var err error
	switch len(values) {
	case 1:
		name = defaultConnectionName()
		config, err = textArg(values[0])
	case 2:
		if name, err = textArg(values[0]); err == nil {
			if err = validateConnectionName(name); err == nil {
				config, err = textArg(values[1])
			}
		}
	default:
		err = errors.New("expected (config) or (connection_name, config)")
	}
	if err != nil {
		ctx.ResultError(err)
		return
	}

	if err := m.setConnectionConfig(name, config); err != nil {
		ctx.ResultError(err)
		return
	}
}

// textArg returns a TEXT or BLOB argument as a string
func textArg(v sqlite.Value) (string, error) {
	switch v.Type() {
	case sqlite.SQLITE_TEXT:
		return v.Text(), nil
	case sqlite.SQLITE_BLOB:
		return string(v.Blob()), nil
	default:
		return "", errors.New("expected a TEXT or BLOB argument")
	}
}

// setConnectionConfig sets the config of the default or a named connection
func (m *ConfigureFn) setConnectionConfig(name, config string) error {
	log.Println("[TRACE] ConfigureFn.setConnectionConfig start")
	defer log.Println("[TRACE] ConfigureFn.setConnectionConfig end")

	logConnection("configuring", name)
	schema, err := upsertConnection(name, config)
	if err != nil {
		return err
	}

	log.Println("[TRACE] ConfigureFn.setConnectionConfig: schema fetched successfully")

	// the tables of the default connection are the eponymous tables: for
	// dynamic backends, refresh them after a config change
	if name == defaultConnectionName() && SCHEMA_MODE_DYNAMIC.Equals(schema.Mode) {
		// drop the existing tables - if they have been created
		if err := m.dropCurrent(); err != nil {
			return err
		}

		// create the tables for the new dynamic schema
		if err := setupTables(schema, m.api); err != nil {
			return err
		}
		currentSchema = schema
	}

	return nil
}

func (m *ConfigureFn) dropCurrent() error {
	if currentSchema != nil {
		sqlite.Register(func(api *sqlite.ExtensionApi) (sqlite.ErrorCode, error) {
			conn := api.Connection()
			for tableName := range currentSchema.GetSchema() {
				log.Println("[TRACE] ConfigureFn.dropCurrent: dropping table", tableName)
				q := fmt.Sprintf("DROP TABLE %s", tableName)
				log.Println("[TRACE] ConfigureFn.dropCurrent: executing query", q)
				err := conn.Exec(q, nil)
				if err != nil {
					log.Println("[ERROR] ConfigureFn.dropCurrent: error dropping table", tableName, err)
					return sqlite.SQLITE_ERROR, err
				}
			}
			return sqlite.SQLITE_OK, nil
		})
	}
	return nil
}

// getSchema returns the schema for the plugin
func getSchema() (*proto.Schema, error) {
	log.Println("[TRACE] getSchema start")
	defer log.Println("[TRACE] getSchema end")

	return getSchemaForConnection(defaultConnectionName())
}

// setupTables sets up the schema tables for the plugin
// it fetched the schema from the plugin and then maps it to SQLite tables
func setupTables(schema *proto.Schema, api *sqlite.ExtensionApi) error {
	log.Println("[TRACE] setupSchemaTables start")
	defer log.Println("[TRACE] setupSchemaTables end")

	// Iterate Tables & Build Modules
	for tableName, tableSchema := range schema.GetSchema() {
		// Translate Schema
		sc := getSQLiteColumnsFromTableSchema(tableSchema)

		current := NewModule(tableName, sc, tableSchema)
		if err := api.CreateModule(tableName, current, sqlite.ReadOnly(true)); err != nil {
			return err
		}
	}
	return nil
}
