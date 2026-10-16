// Huntbase addition: build info for patched builds. Filled in by
// huntbase/build.sh. Do not edit the build info by hand:
// build.sh replaces the placeholder below with the JSON for this build.

package main

import (
	"fmt"
	"strings"

	"go.riyazali.net/sqlite"
)

// huntbaseBuildInfo is JSON, e.g.
// {"build":"r1","plugin":"whois","plugin_version":"v1.2.0","sdk":"v5","steampipe_sqlite":"51d39d2c","fixes":["refilter"]}
// "refilter" means each xFilter call gets a fresh row stream (hb-backend#2573).
// "features":["multi_connection"] means steampipe_configure_<plugin>('<name>', config)
// and declared tables (create virtual table t using <table>(connection='<name>'))
// are supported (turbot/steampipe-sqlite#98). Older builds have no "features".
const huntbaseBuildInfo = `__HUNTBASE_BUILD_INFO__`

type huntbaseBuildFn struct{}

func (huntbaseBuildFn) Args() int           { return 0 }
func (huntbaseBuildFn) Deterministic() bool { return true }
func (huntbaseBuildFn) Apply(ctx *sqlite.Context, _ ...sqlite.Value) {
	ctx.ResultText(huntbaseBuildInfo)
}

// registerHuntbaseBuildFn registers huntbase_build_<plugin>(). The name is per
// plugin because several extensions can be loaded into one connection.
func registerHuntbaseBuildFn(api *sqlite.ExtensionApi) error {
	name := strings.ToLower(fmt.Sprintf("huntbase_build_%s", pluginAlias))
	return api.CreateFunction(name, huntbaseBuildFn{})
}
