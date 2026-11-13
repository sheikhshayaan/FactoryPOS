// Registration code mailer - deploy this ONCE in your own Google account (steps in README, "Registration approval").
// It e-mails the 6-digit code to the account that deploys it. Nothing is stored except a daily counter.
// 1) Change SECRET below to a long random text of your own, 2) Deploy > New deployment > Web app,
//    Execute as: Me, Who has access: Anyone. 3) Copy the web app URL and the SECRET into services/registration_service.py.

const SECRET = "CHANGE-THIS-TO-A-LONG-RANDOM-TEXT";
const MAX_MAILS_PER_DAY = 30;           // protection: nobody can flood your inbox even if the address leaks

function doPost(e) {
  try {
    const d = JSON.parse(e.postData.contents);
    if (d.secret !== SECRET || SECRET.indexOf("CHANGE-THIS") === 0) return out(false, "denied");
    const props = PropertiesService.getScriptProperties();
    const key = "n_" + Utilities.formatDate(new Date(), "UTC", "yyyy-MM-dd");
    const n = parseInt(props.getProperty(key) || "0", 10);
    if (n >= MAX_MAILS_PER_DAY) return out(false, "limit");
    props.setProperty(key, String(n + 1));
    const code = String(d.code || "").replace(/[^0-9]/g, "").slice(0, 6);
    const who = String(d.who || "").slice(0, 120);
    const computer = String(d.computer || "").replace(/[^0-9A-Fa-f-]/g, "").slice(0, 20);
    MailApp.sendEmail(Session.getEffectiveUser().getEmail(),
      "Factory software registration code: " + code,
      "Someone is registering the Factory Management System.\n\nName given: " + who +
      "\nComputer code: " + computer + "\nTime: " + new Date() +
      "\n\nCODE: " + code + "\n\nGive this code ONLY if you know this person. If you do not, ignore this mail.");
    return out(true, "sent");
  } catch (err) {
    return out(false, "error: " + String(err && err.message).slice(0, 80));
  }
}

// Run THIS once from the editor (select "authorize" > Run) and accept the permission screen, otherwise mails cannot be sent.
function authorize() {
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), "Registration mailer is authorised", "The registration mailer can send mail.");
}

function out(ok, msg) {
  return ContentService.createTextOutput(JSON.stringify({ ok: ok, msg: msg })).setMimeType(ContentService.MimeType.JSON);
}
