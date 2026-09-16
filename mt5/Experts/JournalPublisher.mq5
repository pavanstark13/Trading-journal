//+------------------------------------------------------------------+
//|  JournalPublisher.mq5                                             |
//|                                                                  |
//|  Sends this account's trade history to your journal, and keeps    |
//|  it up to date. Install it once and forget it.                    |
//|                                                                  |
//|  It is READ ONLY. It never places, modifies or closes an order,   |
//|  and it never sends your password. It only reads the history the  |
//|  terminal already has and uploads it over HTTPS.                  |
//|                                                                  |
//|  Install: see mt5/README.md                                       |
//+------------------------------------------------------------------+
#property copyright "Trading Journal"
#property version   "1.00"
#property strict

#include <Journal/Crypto.mqh>
#include <Journal/Json.mqh>
#include <Journal/Client.mqh>

//--- inputs -------------------------------------------------------------------
input string InpApiBaseUrl   = "https://api.example.com";  // Your journal's address
input string InpInstallCode  = "";                          // Code from the website
input int    InpSyncSeconds  = 30;                          // How often to check
input bool   InpVerboseLog   = false;

//--- state --------------------------------------------------------------------
CJournalClient g_client;

string   g_apiKeyId  = "";
string   g_apiSecret = "";
long     g_login     = 0;
string   g_server    = "";
datetime g_lastSync  = 0;
datetime g_lastBeat  = 0;
datetime g_nextRetry = 0;
int      g_failures  = 0;
bool     g_dirty     = false;      // a trade happened; sync sooner
bool     g_backfilled = false;
long     g_cursorMsc = 0;          // newest deal the server confirms it holds

#define J_GV_LOCK   "Journal_Lock_"
#define J_BATCH     200            // deals per request
#define J_OVERLAP_S 86400          // re-send the last 24h every sync

//+------------------------------------------------------------------+
int OnInit()
{
   g_login  = AccountInfoInteger(ACCOUNT_LOGIN);
   g_server = AccountInfoString(ACCOUNT_SERVER);

   // Two copies on two charts would upload everything twice. The server would
   // deduplicate it, but refusing is clearer than letting it look broken.
   string lockName = J_GV_LOCK + IntegerToString(g_login);
   if(GlobalVariableCheck(lockName) &&
      (TimeCurrent() - (datetime)GlobalVariableGet(lockName)) < 180)
   {
      Print("Journal: this EA is already running for account ", g_login,
            ". Remove it from the other chart first.");
      return INIT_FAILED;
   }
   GlobalVariableSet(lockName, (double)TimeCurrent());

   if(!LoadCredentials() && !Register())
   {
      Print("Journal: could not connect. Check the install code, and that ",
            InpApiBaseUrl, " is allowed under Tools > Options > Expert Advisors.");
      return INIT_FAILED;
   }

   g_client.Configure(InpApiBaseUrl, g_apiKeyId, g_apiSecret);
   g_client.SetTimeout(30000);          // a backfill batch can be large

   SendHeartbeat();
   EventSetTimer(1);

   Print("Journal connected. Account ", g_login, " @ ", g_server,
         ". Uploading your history now...");
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   GlobalVariableDel(J_GV_LOCK + IntegerToString(g_login));
}

//+------------------------------------------------------------------+
//| A trade just happened. Do NOT upload from here: WebRequest is     |
//| synchronous, and blocking this handler stalls the terminal that   |
//| is managing live money. Just note it and let OnTimer do the work. |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest    &request,
                        const MqlTradeResult     &result)
{
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
      g_dirty = true;
}

//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();
   if(now < g_nextRetry) return;

   if(!g_backfilled)
   {
      Backfill();
      return;
   }

   // A fresh fill syncs within a couple of seconds; otherwise on the normal interval.
   bool due = g_dirty ? (now - g_lastSync >= 2) : (now - g_lastSync >= InpSyncSeconds);
   if(due)
   {
      SyncRecent();
      g_lastSync = now;
   }

   if(now - g_lastBeat >= 30)
   {
      SendHeartbeat();
      g_lastBeat = now;
      GlobalVariableSet(J_GV_LOCK + IntegerToString(g_login), (double)now);
   }
}

//+------------------------------------------------------------------+
//| Upload the entire account history, oldest first, in batches.      |
//| This is what fills a brand new journal with years of trading.     |
//+------------------------------------------------------------------+
void Backfill()
{
   if(!HistorySelect(0, TimeCurrent()))
   {
      Print("Journal: could not read history; will retry.");
      g_nextRetry = TimeCurrent() + 30;
      return;
   }

   int total = HistoryDealsTotal();
   if(total == 0)
   {
      g_backfilled = true;
      Print("Journal: no history on this account yet. Watching for new trades.");
      return;
   }

   int sent = 0;
   int failedAt = -1;

   for(int start = 0; start < total; start += J_BATCH)
   {
      int count = MathMin(J_BATCH, total - start);
      string body = BuildDealBatch(start, count, true);
      if(body == "") continue;

      string response;
      if(!g_client.Post("/api/v1/ea/deals", body, response))
      {
         failedAt = start;
         break;
      }
      sent += count;

      if(InpVerboseLog || start % (J_BATCH * 5) == 0)
         Print("Journal: uploaded ", sent, " of ", total, " history records...");
   }

   if(failedAt >= 0)
   {
      g_failures++;
      g_nextRetry = TimeCurrent() + J_BackoffSeconds(g_failures);
      Print("Journal: upload paused at record ", failedAt, " (",
            g_client.LastError(), "). Retrying shortly; nothing is lost.");
      return;
   }

   g_failures = 0;
   g_backfilled = true;
   g_lastSync = TimeCurrent();
   Print("Journal: history upload complete (", sent, " records). ",
         "Open the website to see your trades.");
}

//+------------------------------------------------------------------+
//| Upload anything recent. Always re-sends the last 24 hours:        |
//| brokers book swap and commission late, so a trade closed          |
//| yesterday can still change. Duplicates are discarded server-side. |
//+------------------------------------------------------------------+
void SyncRecent()
{
   datetime from = TimeCurrent() - J_OVERLAP_S;
   if(g_cursorMsc > 0)
   {
      datetime cursor = (datetime)(g_cursorMsc / 1000) - J_OVERLAP_S;
      if(cursor > 0 && cursor < from) from = cursor;
   }

   if(!HistorySelect(from, TimeCurrent() + 60)) return;

   int total = HistoryDealsTotal();
   if(total == 0)
   {
      g_dirty = false;
      return;
   }

   bool ok = true;
   for(int start = 0; start < total && ok; start += J_BATCH)
   {
      int count = MathMin(J_BATCH, total - start);
      string body = BuildDealBatch(start, count, false);
      if(body == "") continue;

      string response;
      ok = g_client.Post("/api/v1/ea/deals", body, response);
      if(ok)
      {
         long cursor = J_JsonGetLong(response, "cursor", 0);
         if(cursor > g_cursorMsc) g_cursorMsc = cursor;
      }
   }

   if(ok)
   {
      g_failures = 0;
      g_dirty = false;
   }
   else
   {
      g_failures++;
      g_nextRetry = TimeCurrent() + J_BackoffSeconds(g_failures);
      if(g_failures == 1 || g_failures % 10 == 0)
         Print("Journal: sync failed (", g_client.LastError(),
               "). Will retry; no trades are lost.");
   }
}

//+------------------------------------------------------------------+
//| Serialize deals [start, start+count) from the current history     |
//| selection. Everything is sent exactly as the broker reports it;   |
//| no interpretation happens here.                                   |
//+------------------------------------------------------------------+
string BuildDealBatch(const int start, const int count, const bool isBackfill)
{
   string items = "";
   int emitted = 0;

   for(int i = start; i < start + count; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0) continue;

      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      int digits = 5;
      if(symbol != "")
      {
         int d = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
         if(d > 0) digits = d;
      }

      if(emitted > 0) items += ",";
      items += "{";
      items += J_JsonInt("ticket", (long)ticket) + ",";
      items += J_JsonInt("order_ticket",
                         HistoryDealGetInteger(ticket, DEAL_ORDER)) + ",";
      items += J_JsonInt("position_id",
                         HistoryDealGetInteger(ticket, DEAL_POSITION_ID)) + ",";
      items += J_JsonInt("time_msc", HistoryDealGetInteger(ticket, DEAL_TIME_MSC)) + ",";
      items += J_JsonStr("type", DealTypeName(
                         (int)HistoryDealGetInteger(ticket, DEAL_TYPE))) + ",";
      items += J_JsonStr("entry", DealEntryName(
                         (int)HistoryDealGetInteger(ticket, DEAL_ENTRY))) + ",";
      items += J_JsonStr("symbol", symbol) + ",";
      items += J_JsonNum("volume", HistoryDealGetDouble(ticket, DEAL_VOLUME), 2) + ",";
      items += J_JsonNum("price", HistoryDealGetDouble(ticket, DEAL_PRICE), digits) + ",";
      items += J_JsonNum("sl", HistoryDealGetDouble(ticket, DEAL_SL), digits) + ",";
      items += J_JsonNum("tp", HistoryDealGetDouble(ticket, DEAL_TP), digits) + ",";
      items += J_JsonNum("commission",
                         HistoryDealGetDouble(ticket, DEAL_COMMISSION), 2) + ",";
      items += J_JsonNum("swap", HistoryDealGetDouble(ticket, DEAL_SWAP), 2) + ",";
      items += J_JsonNum("profit", HistoryDealGetDouble(ticket, DEAL_PROFIT), 2) + ",";
      items += J_JsonNum("fee", HistoryDealGetDouble(ticket, DEAL_FEE), 2) + ",";
      items += J_JsonInt("magic", HistoryDealGetInteger(ticket, DEAL_MAGIC)) + ",";
      items += J_JsonInt("digits", digits) + ",";
      items += J_JsonStr("reason", DealReasonName(
                         (int)HistoryDealGetInteger(ticket, DEAL_REASON))) + ",";
      items += J_JsonStr("comment", HistoryDealGetString(ticket, DEAL_COMMENT));
      items += "}";
      emitted++;
   }

   if(emitted == 0) return "";
   return "{\"deals\":[" + items + "]," +
          J_JsonBool("is_backfill", isBackfill) + "}";
}

//+------------------------------------------------------------------+
string DealTypeName(const int type)
{
   switch(type)
   {
      case DEAL_TYPE_BUY:                      return "buy";
      case DEAL_TYPE_SELL:                     return "sell";
      case DEAL_TYPE_BALANCE:                  return "balance";
      case DEAL_TYPE_CREDIT:                   return "credit";
      case DEAL_TYPE_CHARGE:                   return "charge";
      case DEAL_TYPE_CORRECTION:               return "correction";
      case DEAL_TYPE_BONUS:                    return "bonus";
      case DEAL_TYPE_COMMISSION:               return "commission";
      case DEAL_TYPE_COMMISSION_DAILY:         return "commission";
      case DEAL_TYPE_COMMISSION_MONTHLY:       return "commission";
      case DEAL_TYPE_COMMISSION_AGENT_DAILY:   return "commission";
      case DEAL_TYPE_COMMISSION_AGENT_MONTHLY: return "commission";
      case DEAL_TYPE_INTEREST:                 return "interest";
      default:                                 return "other";
   }
}

string DealEntryName(const int entry)
{
   switch(entry)
   {
      case DEAL_ENTRY_IN:     return "in";
      case DEAL_ENTRY_OUT:    return "out";
      case DEAL_ENTRY_INOUT:  return "inout";
      case DEAL_ENTRY_OUT_BY: return "out_by";
      default:                return "in";
   }
}

string DealReasonName(const int reason)
{
   switch(reason)
   {
      case DEAL_REASON_CLIENT: return "client";
      case DEAL_REASON_MOBILE: return "mobile";
      case DEAL_REASON_WEB:    return "web";
      case DEAL_REASON_EXPERT: return "expert";
      case DEAL_REASON_SL:     return "sl";
      case DEAL_REASON_TP:     return "tp";
      case DEAL_REASON_SO:     return "so";
      default:                 return "";
   }
}

//+------------------------------------------------------------------+
void SendHeartbeat()
{
   string body = "{";
   body += J_JsonNum("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   body += J_JsonNum("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   body += J_JsonInt("open_positions", PositionsTotal()) + ",";
   body += J_JsonStr("ea_version", "1.00") + ",";
   body += J_JsonInt("terminal_build", TerminalInfoInteger(TERMINAL_BUILD));
   body += "}";

   string response;
   if(!g_client.Post("/api/v1/ea/heartbeat", body, response)) return;

   long cursor = J_JsonGetLong(response, "last_deal_time_msc", 0);
   if(cursor > g_cursorMsc) g_cursorMsc = cursor;
}

//+------------------------------------------------------------------+
bool Register()
{
   if(InpInstallCode == "")
   {
      Print("Journal: paste the install code from the website into InpInstallCode.");
      return false;
   }

   bool hedging = AccountInfoInteger(ACCOUNT_MARGIN_MODE)
                  == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;

   string body = "{";
   body += J_JsonStr("install_code", InpInstallCode) + ",";
   body += J_JsonInt("mt5_login", g_login) + ",";
   body += J_JsonStr("broker_server", g_server) + ",";
   body += J_JsonStr("broker_name", AccountInfoString(ACCOUNT_COMPANY)) + ",";
   body += J_JsonStr("currency", AccountInfoString(ACCOUNT_CURRENCY)) + ",";
   body += J_JsonInt("leverage", AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   // The terminal is the authority here. Margin mode decides how deals are grouped
   // into trades, so a wrong value would quietly distort every statistic.
   body += J_JsonStr("margin_mode", hedging ? "hedging" : "netting") + ",";
   body += J_JsonStr("ea_version", "1.00") + ",";
   body += J_JsonInt("terminal_build", TerminalInfoInteger(TERMINAL_BUILD));
   body += "}";

   g_client.Configure(InpApiBaseUrl, "", "");
   string response;
   if(!g_client.PostUnsigned("/api/v1/ea/register", body, response))
   {
      Print("Journal: registration rejected (", g_client.LastError(), ")");
      return false;
   }

   g_apiKeyId  = J_JsonGet(response, "api_key_id");
   g_apiSecret = J_JsonGet(response, "api_secret");
   if(g_apiKeyId == "" || g_apiSecret == "") return false;

   SaveCredentials();
   return true;
}

//+------------------------------------------------------------------+
//| Credentials live in a terminal file, not in the EA inputs, so a   |
//| shared chart template never carries them.                         |
//+------------------------------------------------------------------+
void SaveCredentials()
{
   int handle = FileOpen(CredentialFile(), FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return;
   FileWriteString(handle, g_apiKeyId + "\n");
   FileWriteString(handle, g_apiSecret + "\n");
   FileClose(handle);
}

bool LoadCredentials()
{
   if(!FileIsExist(CredentialFile())) return false;
   int handle = FileOpen(CredentialFile(), FILE_READ | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE) return false;
   g_apiKeyId  = FileReadString(handle);
   g_apiSecret = FileReadString(handle);
   FileClose(handle);
   return g_apiKeyId != "" && g_apiSecret != "";
}

string CredentialFile()
{
   return "journal_" + IntegerToString(g_login) + ".cred";
}
