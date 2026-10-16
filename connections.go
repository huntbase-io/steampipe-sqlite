package main

import (
	"encoding/json"
	"fmt"
	"log"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/turbot/steampipe-plugin-sdk/v6/grpc/proto"
	"go.riyazali.net/sqlite"
)

// The extension hosts one plugin server, which (like the plugin under the
// Postgres FDW) can serve many connections. Each connection has its own config,
// connection cache and query cache entries: the SDK keys all of them by
// connection name.
//
// The default connection is named after the plugin (e.g. "aws"). It is what
// steampipe_configure_<plugin>(config) sets and what the eponymous tables
// (select * from aws_account) read. Named connections are added with
// steampipe_configure_<plugin>('<name>', config) and read through declared
// tables: create virtual table prod_account using aws_account(connection='prod').

// connectionNamePattern limits names to something safe to log and to use in
// cache keys.
var connectionNamePattern = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)

type connectionRegistry struct {
	mu sync.RWMutex
	// connection name -> schema for that connection
	schemas map[string]*proto.Schema
}

var connections = &connectionRegistry{schemas: map[string]*proto.Schema{}}

func (r *connectionRegistry) get(name string) (*proto.Schema, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.schemas[name]
	return s, ok
}

func (r *connectionRegistry) set(name string, schema *proto.Schema) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.schemas[name] = schema
}

func (r *connectionRegistry) names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	names := make([]string, 0, len(r.schemas))
	for name := range r.schemas {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func defaultConnectionName() string { return pluginAlias }

func validateConnectionName(name string) error {
	if !connectionNamePattern.MatchString(name) {
		return fmt.Errorf("invalid connection name %q: use letters, digits, '_', '-' or '.' (at most 128 characters, not starting with '-' or '.')", name)
	}
	return nil
}

func newConnectionConfig(name, config string) *proto.ConnectionConfig {
	pluginName := fmt.Sprintf("steampipe-plugin-%s", pluginAlias)
	return &proto.ConnectionConfig{
		Connection:      name,
		Plugin:          pluginName,
		PluginShortName: pluginAlias,
		Config:          config,
		PluginInstance:  pluginName,
	}
}

// upsertConnection adds the named connection to the plugin server, or applies
// a new config to it, and records its schema.
//
// A known connection is updated with UpdateConnectionConfigs so the plugin's
// ConnectionConfigChangedFunc runs; by default that clears the connection
// cache and query cache of that connection only. A new connection is added the
// same way, which also registers its schema with the query cache.
//
// The plugin server's UpdateConnectionConfigs does not return the connections
// it failed to parse (the SDK drops them from the response), so the config is
// then passed to SetAllConnectionConfigs, which does report them. That call
// re-applies the same config to the same connection and leaves the query cache
// alone (MaxCacheSizeMb -1). Without it an invalid config is silently ignored
// and the connection keeps its previous config.
func upsertConnection(name, config string) (*proto.Schema, error) {
	c := newConnectionConfig(name, config)
	_, known := connections.get(name)

	update := &proto.UpdateConnectionConfigsRequest{}
	if known {
		update.Changed = []*proto.ConnectionConfig{c}
	} else {
		update.Added = []*proto.ConnectionConfig{c}
	}
	if _, err := pluginServer.UpdateConnectionConfigs(update); err != nil {
		return nil, err
	}

	res, err := pluginServer.SetAllConnectionConfigs(&proto.SetAllConnectionConfigsRequest{
		Configs:        []*proto.ConnectionConfig{c},
		MaxCacheSizeMb: -1,
	})
	if res != nil {
		if msg, failed := res.FailedConnections[name]; failed {
			return nil, fmt.Errorf("connection %q: %s", name, msg)
		}
	}
	if err != nil {
		return nil, err
	}

	schema, err := getSchemaForConnection(name)
	if err != nil {
		return nil, err
	}
	connections.set(name, schema)
	return schema, nil
}

// getSchemaForConnection returns the plugin schema as seen by one connection.
// For static plugins it is the same for every connection.
func getSchemaForConnection(name string) (*proto.Schema, error) {
	s, err := pluginServer.GetSchema(&proto.GetSchemaRequest{Connection: name})
	if err != nil {
		return nil, err
	}
	return s.GetSchema(), nil
}

// parseModuleArgs reads the arguments of
// create virtual table t using <table>(connection='name').
// SQLite passes the module name, the schema name and the table name first;
// the remaining arguments are the ones written in parentheses.
func parseModuleArgs(args []string) (connection string, err error) {
	if len(args) <= 3 {
		return "", nil
	}
	for _, arg := range args[3:] {
		key, value, ok := strings.Cut(arg, "=")
		if !ok {
			return "", fmt.Errorf("unexpected argument %q: expected connection='<name>'", strings.TrimSpace(arg))
		}
		key = strings.ToLower(strings.TrimSpace(key))
		if key != "connection" {
			return "", fmt.Errorf("unknown argument %q: the only argument is connection='<name>'", key)
		}
		value = unquoteModuleArg(strings.TrimSpace(value))
		if err := validateConnectionName(value); err != nil {
			return "", err
		}
		connection = value
	}
	return connection, nil
}

func unquoteModuleArg(v string) string {
	if len(v) >= 2 {
		first, last := v[0], v[len(v)-1]
		if (first == '\'' && last == '\'') || (first == '"' && last == '"') {
			q := string(first)
			return strings.ReplaceAll(v[1:len(v)-1], q+q, q)
		}
	}
	return v
}

// ConnectionsFn implements steampipe_connections_<plugin>(), which returns
// the configured connection names as a JSON array.
type ConnectionsFn struct{}

func (ConnectionsFn) Args() int           { return 0 }
func (ConnectionsFn) Deterministic() bool { return false }
func (ConnectionsFn) Apply(ctx *sqlite.Context, _ ...sqlite.Value) {
	b, err := json.Marshal(connections.names())
	if err != nil {
		ctx.ResultError(err)
		return
	}
	ctx.ResultText(string(b))
	ctx.ResultSubType(74) // JSON
}

func logConnection(action, name string) {
	// never log the config: it holds credentials
	log.Printf("[INFO] %s connection %q", action, name)
}
