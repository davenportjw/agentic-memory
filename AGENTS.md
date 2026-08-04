# Agent & System Instructions for Developer Assistants (`electric-sheep`)

## CRITICAL OPERATIONAL RULE: Deployed State Testing & Deployment as Final Step
1. **Test in Deployed State**: Any code, UI, agent instruction, or prompt changes MUST be tested in the deployed state on Google Cloud Run. Local testing (`uv run pytest`) is required, but it is not sufficient on its own.
2. **Deployment as the Final Step**: Deployment to Google Cloud Run using `./scripts/deploy_cloud_run_source.sh` (or CI/CD) MUST always be the final step of any development, feature, or bugfix workflow.
3. **Validate Live Production Endpoints**: After deploying to Google Cloud Run, always execute `python3 -m scripts.validate_live_endpoints` to verify that all 7 live REST API endpoints respond correctly in the production environment (`davenport-boutique`).
4. **UI Architecture & Layout Integrity (Using `DESIGN.md`)**: When creating or modifying UI pages or navigation in `electric-sheep`:
   - Always adhere to the layout rules in `DESIGN.md` Section 3.1.1 (Top App Bar 2-Child Standard, ensuring `.app-nav` is inside `.app-title-group`) and Section 3.1.2 (`.app-body-layout` consistency across all pages).
   - Verify layout integrity by running the automated DOM verification tests in `tests/test_ui_service.py` (`test_ui_service_topbar_and_layout_integrity`) to assert that the top bar never wraps into an ugly layout and navigation between pages never breaks layout structure.
