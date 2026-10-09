# Classroom workflow

## How to use

1. Visit `/admin` (redirects to `/admin/login`)
2. Continue with the personal Microsoft account configured in `ADMIN_EMAIL`
3. Approve each teacher by entering their personal Microsoft account email in the control panel
4. Teachers sign in with the approved Microsoft account, then create and manage only their own sessions
5. Upload whitelist CSV (`student_id,name`) for that session
6. Share the session's QR code or join URL (`https://public-goods.azurewebsites.net/join/<join-token>`). Students can scan the code or open the URL directly, then enter the student number and name on the whitelist.
7. Lock groups (random assignment; groups constrained to 3-7 students, target 5)
8. For each round:
   - Choose Baseline, Punishment, or Reward, then open the current round; the choice is locked once the round opens
   - Students submit contribution (`0-10`)
   - Baseline round: close and compute directly
   - Reward/Punishment round: open action stage, let students submit/update actions, then compute when ready
   - Each action point costs 1 token. A student's total action cost is capped at 5 tokens and cannot exceed the tokens left from the round endowment after contributing; public returns and received action effects are not spendable during that round
9. Use `/display/<session_id>` for classroom projection
10. Export CSV from the session admin panel (long format: one row per student per round)
