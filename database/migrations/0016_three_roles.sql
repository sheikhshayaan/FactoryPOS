-- The program now has three roles: owner, accountant, auditor. People who still have one of the two removed roles are switched OFF (not deleted):
-- nothing they recorded is touched, and the owner can create a new user for them with one of the three roles.
UPDATE users SET is_active=0 WHERE role IN ('inventory_manager','production_supervisor');
