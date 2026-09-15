//+------------------------------------------------------------------+
//|  MemberTradeCopier.mq5                                            |
//|  Executes copy instructions authorized by the TradeBridge backend |
//|  on a MEMBER account.                                             |
//|                                                                  |
//|  THIS EA HAS NO STRATEGY. It never invents a trade, never decides |
//|  a volume, and never re-executes without a fresh server-issued    |
//|  execution token. Every instruction it acts on was risk-checked   |
//|  and sized on the server.                                         |
//+------------------------------------------------------------------+
#property copyright "TradeBridge"
#property version   "1.00"
#property strict

#include <TradeBridge/Crypto.mqh>
#include <TradeBridge/Json.mqh>
#include <TradeBridge/Client.mqh>

#include <Trade/Trade.mqh>

//--- inputs -------------------------------------------------------------------
input string InpApiBaseUrl     = "https://api.example.com";  // Backend base URL
input string InpInstallCode    = "";                          // One-time install code
input int    InpPollWaitSecs   = 25;                          // Long-poll wait
input int    InpHeartbeatSecs  = 30;                          // Heartbeat interval
input int    InpHaltGraceSecs  = 120;                         // Stop trading if offline longer
input bool   InpVerboseLog     = false;

//--- state --------------------------------------------------------------------
CTradeBridgeClient g_client;
CTrade             g_trade;

string   g_apiKeyId  = "";
string   g_apiSecret = "";
long     g_login     = 0;
string   g_server    = "";
datetime g_lastBeat  = 0;
datetime g_lastOk    = 0;
bool     g_halted    = false;
int      g_failures  = 0;
datetime g_nextPollAt = 0;

#define TB_GV_LOCK "TradeBridge_Member_Lock_"

//+------------------------------------------------------------------+
int OnInit()
{
   g_login  = AccountInfoInteger(ACCOUNT_LOGIN);
   g_server = AccountInfoString(ACCOUNT_SERVER);

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      Print("TradeBridge: 'Allow Algo Trading' is off in the terminal. "
            "The copier cannot execute.");
      return INIT_FAILED;
   }

   string lockName = TB_GV_LOCK + IntegerToString(g_login);
   if(GlobalVariableCheck(lockName) &&
      (TimeCurrent() - (datetime)GlobalVariableGet(lockName)) < 120)
   {
      Print("TradeBridge: another MemberTradeCopier is running for account ",
            g_login, ". Remove it from the other chart first.");
      return INIT_FAILED;
   }
   GlobalVariableSet(lockName, (double)TimeCurrent());

   if(!LoadCredentials() && !Register())
   {
      Print("TradeBridge: registration failed. Check the install code and the "
            "WebRequest URL whitelist.");
      return INIT_FAILED;
   }

   g_client.Configure(InpApiBaseUrl, g_apiKeyId, g_apiSecret);
   // The poll blocks server-side for up to InpPollWaitSecs, so the HTTP timeout
   // must be comfortably longer or an empty poll looks like a network failure.
   g_client.SetTimeout((InpPollWaitSecs + 10) * 1000);

   g_trade.SetAsyncMode(false);
   g_lastOk = TimeCurrent();

   SendHeartbeat();
   EventSetTimer(1);
   Print("TradeBridge member copier active. Account ", g_login, " @ ", g_server);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   GlobalVariableDel(TB_GV_LOCK + IntegerToString(g_login));
}

//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();

   if((now - g_lastBeat) >= InpHeartbeatSecs)
   {
      SendHeartbeat();
      g_lastBeat = now;
      GlobalVariableSet(TB_GV_LOCK + IntegerToString(g_login), (double)now);
   }

   // Fail safe, not fail open: if the backend has been unreachable for longer than
   // the grace period we stop executing rather than trading blind. An emergency stop
   // we cannot hear about is exactly the case this protects.
   if((now - g_lastOk) > InpHaltGraceSecs)
   {
      if(!g_halted)
      {
         g_halted = true;
         Print("TradeBridge: backend unreachable for ", (now - g_lastOk),
               "s. Refusing new instructions until contact is restored.");
      }
   }

   if(now >= g_nextPollAt)
      Poll();
}

//+------------------------------------------------------------------+
void Poll()
{
   string response;
   string path = "/api/v1/ea/member/poll?wait=" + IntegerToString(InpPollWaitSecs);

   if(!g_client.Get(path, response, (InpPollWaitSecs + 10) * 1000))
   {
      g_failures++;
      g_nextPollAt = TimeCurrent() + TB_BackoffSeconds(g_failures);
      if(g_failures == 1 || g_failures % 10 == 0)
         Print("TradeBridge: poll failed (", g_client.LastError(), ")");
      return;
   }

   g_failures = 0;
   g_lastOk   = TimeCurrent();
   g_nextPollAt = TimeCurrent();
   g_halted   = false;

   if(TB_JsonGetBool(response, "halt", false))
   {
      if(!g_halted) Print("TradeBridge: server reports HALT. No instructions will run.");
      g_halted = true;
      return;
   }

   string instructions[];
   int count = TB_JsonObjects(response, "instructions", instructions);
   for(int i = 0; i < count; i++)
      Execute(instructions[i]);
}

//+------------------------------------------------------------------+
//| Execute one authorized instruction, then always report the truth. |
//| An unreported instruction is worse than a failed one, because the |
//| dashboard would then be lying to the operator.                    |
//+------------------------------------------------------------------+
void Execute(const string instruction)
{
   string token     = TB_JsonGet(instruction, "execution_token");
   string action    = TB_JsonGet(instruction, "action");
   string symbol    = TB_JsonGet(instruction, "symbol");
   string side      = TB_JsonGet(instruction, "side");
   string clientTag = TB_JsonGet(instruction, "client_tag");
   string expiresAt = TB_JsonGet(instruction, "expires_at");
   double lot       = TB_JsonGetDouble(instruction, "lot");
   double stopLoss  = TB_JsonGetDouble(instruction, "stop_loss");
   double takeProfit= TB_JsonGetDouble(instruction, "take_profit");
   int    slippage  = (int)TB_JsonGetLong(instruction, "max_slippage_points", 20);
   int    maxSpread = (int)TB_JsonGetLong(instruction, "max_spread_points", 0);

   if(token == "") return;

   if(g_halted)
   {
      Report(token, "SKIPPED", 0, 0, 0, 0, "Halted by backend");
      return;
   }

   // The lease is the staleness guard. Executing a stale entry at a price that has
   // moved is worse than not executing at all.
   if(IsExpired(expiresAt))
   {
      Report(token, "SKIPPED", 0, 0, 0, 0, "Lease expired before execution");
      return;
   }

   // Last line of defence against duplicate fills: if a position or order already
   // carries this tag, a previous attempt succeeded even if we never got to report it.
   if(HasTag(clientTag))
   {
      Report(token, "SKIPPED", 0, 0, 0, 0, "Already executed (client tag present)");
      return;
   }

   if(!SymbolSelect(symbol, true) ||
      SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_DISABLED)
   {
      Report(token, "REJECTED", 0, 0, 0, 0, "Symbol unavailable: " + symbol);
      return;
   }

   if(maxSpread > 0)
   {
      long spread = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
      if(spread > maxSpread)
      {
         Report(token, "REJECTED", 0, 0, 0, 0,
                StringFormat("Spread %d exceeds limit %d", (int)spread, maxSpread));
         return;
      }
   }

   g_trade.SetDeviationInPoints(slippage);
   g_trade.SetExpertMagicNumber(0);

   bool ok = false;
   if(action == "OPEN")
   {
      // Re-normalize against live broker specs: contract specifications change, and
      // the server's copy was computed from whatever it last knew.
      lot = NormalizeLot(symbol, lot);
      if(lot <= 0)
      {
         Report(token, "REJECTED", 0, 0, 0, 0, "Lot below broker minimum after normalization");
         return;
      }
      ok = (side == "BUY")
           ? g_trade.Buy(lot, symbol, 0.0, stopLoss, takeProfit, clientTag)
           : g_trade.Sell(lot, symbol, 0.0, stopLoss, takeProfit, clientTag);
   }
   else if(action == "CLOSE")
   {
      ok = CloseTagged(symbol, clientTag, 0.0);
   }
   else if(action == "PARTIAL_CLOSE")
   {
      ok = CloseTagged(symbol, clientTag, NormalizeLot(symbol, lot));
   }
   else if(action == "MODIFY")
   {
      ok = ModifyTagged(symbol, stopLoss, takeProfit);
   }
   else
   {
      Report(token, "SKIPPED", 0, 0, 0, 0, "Unsupported action: " + action);
      return;
   }

   uint   retcode = g_trade.ResultRetcode();
   double price   = g_trade.ResultPrice();
   double volume  = g_trade.ResultVolume();
   long   ticket  = (long)g_trade.ResultOrder();

   if(ok && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_PLACED ||
             retcode == TRADE_RETCODE_DONE_PARTIAL))
   {
      Report(token, "EXECUTED", ticket, price, volume, (int)retcode, "");
      if(InpVerboseLog)
         Print("TradeBridge executed ", action, " ", symbol, " ", volume, " @ ", price);
   }
   else
   {
      Report(token, "FAILED", ticket, price, volume, (int)retcode,
             g_trade.ResultRetcodeDescription());
      Print("TradeBridge execution failed: ", action, " ", symbol,
            " retcode=", retcode, " ", g_trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
void Report(const string token, const string status, const long ticket,
            const double price, const double volume, const int retcode,
            const string message)
{
   string body = "{";
   body += TB_JsonStr("execution_token", token) + ",";
   body += TB_JsonStr("status", status) + ",";
   body += (ticket > 0 ? TB_JsonInt("broker_ticket", ticket)
                       : TB_JsonNull("broker_ticket")) + ",";
   body += (price > 0 ? TB_JsonNum("execution_price", price, 5)
                      : TB_JsonNull("execution_price")) + ",";
   body += TB_JsonNum("executed_volume", volume, 2) + ",";
   body += TB_JsonInt("broker_retcode", retcode) + ",";
   body += TB_JsonStr("message", message) + ",";
   body += TB_JsonStr("executed_at", TB_IsoUtc((long)TimeGMT() * 1000));
   body += "}";

   string response;
   if(!g_client.Post("/api/v1/ea/member/result", body, response))
      Print("TradeBridge: failed to report result for token ",
            StringSubstr(token, 0, 10), " (", g_client.LastError(), ")");
}

//+------------------------------------------------------------------+
bool HasTag(const string clientTag)
{
   if(clientTag == "") return false;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_COMMENT) == clientTag) return true;
   }
   for(int i = 0; i < OrdersTotal(); i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      if(OrderGetString(ORDER_COMMENT) == clientTag) return true;
   }
   return false;
}

//+------------------------------------------------------------------+
bool CloseTagged(const string symbol, const string clientTag, const double volume)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;

      double open = PositionGetDouble(POSITION_VOLUME);
      if(volume > 0 && volume < open)
         return g_trade.PositionClosePartial(ticket, volume);
      return g_trade.PositionClose(ticket);
   }
   return false;
}

//+------------------------------------------------------------------+
bool ModifyTagged(const string symbol, const double stopLoss, const double takeProfit)
{
   bool any = false;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      if(g_trade.PositionModify(ticket, stopLoss, takeProfit)) any = true;
   }
   return any;
}

//+------------------------------------------------------------------+
double NormalizeLot(const string symbol, const double requested)
{
   double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double step    = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0) step = 0.01;

   double snapped = MathFloor(requested / step) * step;
   snapped = NormalizeDouble(snapped, 2);

   if(snapped < minLot) return 0.0;      // never round UP into more risk
   if(snapped > maxLot) return maxLot;
   return snapped;
}

//+------------------------------------------------------------------+
bool IsExpired(const string iso)
{
   if(iso == "") return false;
   // "2026-09-15T08:15:22Z" -> "2026.09.15 08:15:22"
   string normalized = iso;
   StringReplace(normalized, "-", ".");
   StringReplace(normalized, "T", " ");
   StringReplace(normalized, "Z", "");
   int dot = StringFind(normalized, ".", 14);
   if(dot > 0) normalized = StringSubstr(normalized, 0, dot);
   datetime expiry = StringToTime(normalized);
   if(expiry == 0) return false;
   return TimeGMT() > expiry;
}

//+------------------------------------------------------------------+
void SendHeartbeat()
{
   string body = "{";
   body += TB_JsonNum("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   body += TB_JsonNum("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   body += TB_JsonNum("free_margin", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ",";
   body += TB_JsonInt("open_positions", PositionsTotal()) + ",";
   body += TB_JsonStr("ea_version", "1.00") + ",";
   body += TB_JsonInt("terminal_build", TerminalInfoInteger(TERMINAL_BUILD));
   body += "}";

   string response;
   if(!g_client.Post("/api/v1/ea/member/heartbeat", body, response)) return;

   g_lastOk = TimeCurrent();
   if(TB_JsonGetBool(response, "emergency_stop", false))
   {
      if(!g_halted) Print("TradeBridge: emergency stop is active.");
      g_halted = true;
   }
   else
   {
      g_halted = false;
   }
}

//+------------------------------------------------------------------+
bool Register()
{
   if(InpInstallCode == "")
   {
      Print("TradeBridge: InpInstallCode is empty. Ask your administrator for one.");
      return false;
   }

   string body = "{";
   body += TB_JsonStr("install_code", InpInstallCode) + ",";
   body += TB_JsonStr("kind", "MEMBER") + ",";
   body += TB_JsonInt("mt5_login", g_login) + ",";
   body += TB_JsonStr("broker_server", g_server) + ",";
   body += TB_JsonStr("currency", AccountInfoString(ACCOUNT_CURRENCY)) + ",";
   body += TB_JsonInt("leverage", AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   body += TB_JsonStr("margin_mode",
            AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
            ? "hedging" : "netting") + ",";
   body += TB_JsonStr("ea_version", "1.00") + ",";
   body += TB_JsonInt("terminal_build", TerminalInfoInteger(TERMINAL_BUILD));
   body += "}";

   g_client.Configure(InpApiBaseUrl, "", "");
   string response;
   if(!g_client.PostUnsigned("/api/v1/ea/register", body, response))
   {
      Print("TradeBridge registration rejected: ", g_client.LastError());
      return false;
   }

   g_apiKeyId  = TB_JsonGet(response, "api_key_id");
   g_apiSecret = TB_JsonGet(response, "api_secret");
   if(g_apiKeyId == "" || g_apiSecret == "") return false;

   SaveCredentials();
   Print("TradeBridge registered successfully.");
   return true;
}

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
   return "tradebridge_member_" + IntegerToString(g_login) + ".cred";
}
