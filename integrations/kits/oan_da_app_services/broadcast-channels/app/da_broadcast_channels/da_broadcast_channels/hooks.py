app_name = "da_broadcast_channels"
app_title = "DA Broadcast Channels"
app_publisher = "OpenAgriNet"
app_description = "SMS and Telegram delivery of DA web app broadcasts (Channels Specification §6.8)"
app_email = "admin@openagrinet.org"
app_license = "MIT"

# Installed next to da_services; da_services itself is not changed.
required_apps = ["da_services", "oan_messaging_connector"]

# Adds POST/GET /api/v1/da-services/broadcasts after DA Services registers its routes.
before_request = ["da_broadcast_channels.api.ensure_routes_registered"]

scheduler_events = {
	"cron": {
		# Re-enters unfinished dispatches: waits for Telegram, then resumes the SMS pass.
		"* * * * *": ["da_broadcast_channels.dispatch.resume_open"],
	},
}
