output "gemini_api_key_secret_id" {
  description = "Secret Manager secret ID for Gemini API key"
  value       = google_secret_manager_secret.gemini_api_key.secret_id
}

output "gemini_api_key_secret_name" {
  description = "Full resource name of the Gemini API key secret"
  value       = google_secret_manager_secret.gemini_api_key.name
}

output "resend_api_key_secret_id" {
  description = "Secret Manager secret ID for Resend API key"
  value       = google_secret_manager_secret.resend_api_key.secret_id
}

output "resend_api_key_secret_name" {
  description = "Full resource name of the Resend API key secret"
  value       = google_secret_manager_secret.resend_api_key.name
}
