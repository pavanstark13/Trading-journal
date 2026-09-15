//+------------------------------------------------------------------+
//|  MasterTradeBridge.mq5                                            |
//|  Detects trade-server transactions on the MASTER account and      |
//|  delivers them to the TradeBridge backend over signed HTTPS.      |
//|                                                                  |
//|  THIS EA CONTAINS NO BUSINESS LOGIC. It is a secure bridge.       |
//|  Filtering, formatting, copying and risk all live on the server,  |
//|  where they can be tested, changed and audited.                   |
//|                                                                  |
//|  Install: see MT5_INTEGRATION.md section 6.                       |
//+------------------------------------------------------------------+
#property copyright "TradeBridge"
#property version   "1.00"
#property strict

#include <TradeBridge/Crypto.mqh>
#include <TradeBridge/EventId.mqh>
#include <TradeBridge/Json.mqh>
#include <TradeBridge/Client.mqh>

//--- inputs -------------------------------------------------------------------
input string InpApiBaseUrl    = "https://api.example.com";  // Backend base URL
input string InpInstallCode   = "";                          // One-time install code
input string InpNamespace     = "tradebridge.example.com";   // MUST match server config
input int    InpFlushSeconds  = 1;                           // Event flush interval
input int    InpHeartbeatSecs = 30;                          // Heartbeat interval
input int    InpBatchMax      = 50;                          // Events per request
input bool   InpVerboseLog    = false;

//--- a queued, not-yet-delivered event -----------------------------------------
struct TBEvent
{
   string eventId;
   string eventType;
   string symbol;
   string side;
   string comment;
   double volume;
   double price;
   double stopLoss;
   double takeProfit;
   double prevStopLoss;
   double prevTakeProfit;
   double profit;
   double commission;
   double swap;
   long   ticket;
   long   positionId;
   long   orderTicket;
   long   dealTicket;
   long   magic;
   long   occurredAtMs;
};

//--- cached SL/TP so modifications can be detected and diffed ------------------
struct TBPositionSnapshot
{
   long   positionId;
   double stopLoss;
   double takeProfit;
   double volume;
};

//--- state --------------------------------------------------------------------
CTradeBridgeClient g_client;
CTradeBridgeSpool  g_spool;

TBEvent            g_queue[];
TBPositionSnapshot g_snapshots[];
uchar              g_namespace[16];

string   g_apiKeyId    = "";
string   g_apiSecret   = "";
long     g_login       = 0;
string   g_server      = "";
datetime g_lastFlush   = 0;
datetime g_lastBeat    = 0;
datetime g_nextRetryAt = 0;
int      g_failures    = 0;
bool     g_registered  = false;

#define TB_GV_KEY    "TradeBridge_Master_Key_"
#define TB_GV_LOCK   "TradeBridge_Master_Lock_"

//+------------------------------------------------------------------+
int OnInit()
{
   g_login  = AccountInfoInteger(ACCOUNT_LOGIN);
   g_server = AccountInfoString(ACCOUNT_SERVER);

   // Attaching to two charts would double every event. Idempotency absorbs it, but
   // refusing outright is clearer than letting the operator wonder.
   string lockName = TB_GV_LOCK + IntegerToString(g_login);
   if(GlobalVariableCheck(lockName) &&
      (TimeCurrent() - (datetime)GlobalVariableGet(lockName)) < 120)
   {
      Print("TradeBridge: another MasterTradeBridge instance is already running for "
            "account ", g_login, ". Remove it from the other chart first.");
      return INIT_FAILED;
   }
   GlobalVariableSet(lockName, (double)TimeCurrent());

   TB_NamespaceUuid(InpNamespace, g_namespace);
   g_spool.Configure("tradebridge_master_" + IntegerToString(g_login) + ".jsonl");

   if(!LoadCredentials() && !Register())
   {
      Print("TradeBridge: registration failed. Check the install code and the "
            "WebRequest URL whitelist, then reload the EA.");
      return INIT_FAILED;
   }

   g_client.Configure(InpApiBaseUrl, g_apiKeyId, g_apiSecret);
   g_registered = true;

   RebuildSnapshots();
   RestoreSpool();
   SendHeartbeat();

   EventSetTimer(1);
   Print("TradeBridge master bridge active. Account ", g_login, " @ ", g_server);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   PersistQueue();          // never lose buffered events on a reload
   GlobalVariableDel(TB_GV_LOCK + IntegerToString(g_login));
}

//+------------------------------------------------------------------+
//|  THE HOT PATH.                                                    |
//|  This handler must return in microseconds. WebRequest() is        |
//|  synchronous; calling it here would stall the terminal that is    |
//|  managing live money, and MT5 will drop subsequent callbacks.     |
//|  So: classify, queue, return. Delivery happens in OnTimer().      |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest    &request,
                        const MqlTradeResult     &result)
{
   switch(trans.type)
   {
      case TRADE_TRANSACTION_DEAL_ADD:
         HandleDeal(trans);
         break;

      case TRADE_TRANSACTION_POSITION:
         HandlePositionChange(trans);
         break;

      case TRADE_TRANSACTION_ORDER_ADD:
         if(IsPendingOrderType(trans.order_type))
            QueueOrderEvent("PENDING_ORDER_CREATED", trans);
         break;

      case TRADE_TRANSACTION_ORDER_UPDATE:
         if(IsPendingOrderType(trans.order_type))
            QueueOrderEvent("PENDING_ORDER_MODIFIED", trans);
         break;

      case TRADE_TRANSACTION_ORDER_DELETE:
         if(IsPendingOrderType(trans.order_type))
            QueueOrderEvent("PENDING_ORDER_CANCELLED", trans);
         break;

      // TRADE_TRANSACTION_REQUEST is an echo of what we asked for, not a fact.
      // Publishing it would create phantom signals for rejected orders.
      default:
         break;
   }
}

//+------------------------------------------------------------------+
void OnTimer()
{
   datetime now = TimeCurrent();

   if(now >= g_nextRetryAt && ArraySize(g_queue) > 0 &&
      (now - g_lastFlush) >= InpFlushSeconds)
   {
      FlushQueue();
      g_lastFlush = now;
   }

   if((now - g_lastBeat) >= InpHeartbeatSecs)
   {
      SendHeartbeat();
      g_lastBeat = now;
      GlobalVariableSet(TB_GV_LOCK + IntegerToString(g_login), (double)now);
   }
}

//+------------------------------------------------------------------+
//| Deal classification. See MT5_INTEGRATION.md section 4.            |
//+------------------------------------------------------------------+
void HandleDeal(const MqlTradeTransaction &trans)
{
   if(!HistoryDealSelect(trans.deal)) return;

   long   entry      = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   long   dealType   = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   long   positionId = HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   double volume     = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
   double price      = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
   string symbol     = HistoryDealGetString(trans.deal, DEAL_SYMBOL);
   long   occurredMs = HistoryDealGetInteger(trans.deal, DEAL_TIME_MSC);

   // Balance, credit and correction deals are account adjustments, not trades.
   if(dealType != DEAL_TYPE_BUY && dealType != DEAL_TYPE_SELL) return;

   string side = (dealType == DEAL_TYPE_BUY) ? "BUY" : "SELL";

   if(entry == DEAL_ENTRY_IN)
   {
      QueueDealEvent("TRADE_OPENED", trans, side, symbol, volume, price,
                     positionId, occurredMs);
      UpdateSnapshot(positionId);
   }
   else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
   {
      // Remaining volume decides whether this closed the position or scaled out.
      bool stillOpen = PositionSelectByTicket(positionId);
      QueueDealEvent(stillOpen ? "TRADE_PARTIAL_CLOSED" : "TRADE_CLOSED",
                     trans, side, symbol, volume, price, positionId, occurredMs);
      if(!stillOpen) RemoveSnapshot(positionId);
   }
   else if(entry == DEAL_ENTRY_INOUT)
   {
      // Netting reversal: ONE deal that both closes and opens. Two events, or the
      // server would see a single trade where the account actually flipped side.
      QueueDealEvent("TRADE_CLOSED", trans, side == "BUY" ? "SELL" : "BUY",
                     symbol, volume, price, positionId, occurredMs);
      QueueDealEvent("TRADE_OPENED", trans, side, symbol, volume, price,
                     positionId, occurredMs);
      UpdateSnapshot(positionId);
   }
}

//+------------------------------------------------------------------+
void HandlePositionChange(const MqlTradeTransaction &trans)
{
   long positionId = trans.position;
   if(!PositionSelectByTicket(positionId)) return;

   double stopLoss   = PositionGetDouble(POSITION_SL);
   double takeProfit = PositionGetDouble(POSITION_TP);
   double volume     = PositionGetDouble(POSITION_VOLUME);

   int index = FindSnapshot(positionId);
   double prevSl = (index >= 0) ? g_snapshots[index].stopLoss   : 0.0;
   double prevTp = (index >= 0) ? g_snapshots[index].takeProfit : 0.0;

   if(index >= 0 &&
      MathAbs(prevSl - stopLoss)   < 0.0000001 &&
      MathAbs(prevTp - takeProfit) < 0.0000001)
      return;                                   // nothing actually changed

   TBEvent event;
   ZeroEvent(event);
   event.eventType      = "TRADE_MODIFIED";
   event.symbol         = PositionGetString(POSITION_SYMBOL);
   event.side           = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                          ? "BUY" : "SELL";
   event.volume         = volume;
   event.price          = PositionGetDouble(POSITION_PRICE_OPEN);
   event.stopLoss       = stopLoss;
   event.takeProfit     = takeProfit;
   event.prevStopLoss   = prevSl;
   event.prevTakeProfit = prevTp;
   event.positionId     = positionId;
   event.ticket         = positionId;
   event.magic          = PositionGetInteger(POSITION_MAGIC);
   event.comment        = PositionGetString(POSITION_COMMENT);
   event.occurredAtMs   = NowMs();

   AssignEventId(event);
   Enqueue(event);
   UpdateSnapshot(positionId);
}

//+------------------------------------------------------------------+
void QueueDealEvent(const string eventType, const MqlTradeTransaction &trans,
                    const string side, const string symbol, const double volume,
                    const double price, const long positionId, const long occurredMs)
{
   TBEvent event;
   ZeroEvent(event);
   event.eventType    = eventType;
   event.symbol       = symbol;
   event.side         = side;
   event.volume       = volume;
   event.price        = price;
   event.positionId   = positionId;
   event.dealTicket   = (long)trans.deal;
   event.orderTicket  = (long)trans.order;
   event.ticket       = positionId;
   event.occurredAtMs = occurredMs > 0 ? occurredMs : NowMs();
   event.magic        = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
   event.comment      = HistoryDealGetString(trans.deal, DEAL_COMMENT);
   event.profit       = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);
   event.commission   = HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   event.swap         = HistoryDealGetDouble(trans.deal, DEAL_SWAP);

   if(PositionSelectByTicket(positionId))
   {
      event.stopLoss   = PositionGetDouble(POSITION_SL);
      event.takeProfit = PositionGetDouble(POSITION_TP);
   }

   AssignEventId(event);
   Enqueue(event);
}

//+------------------------------------------------------------------+
void QueueOrderEvent(const string eventType, const MqlTradeTransaction &trans)
{
   TBEvent event;
   ZeroEvent(event);
   event.eventType    = eventType;
   event.symbol       = trans.symbol;
   event.side         = IsBuyOrderType(trans.order_type) ? "BUY" : "SELL";
   event.volume       = trans.volume;
   event.price        = trans.price;
   event.stopLoss     = trans.price_sl;
   event.takeProfit   = trans.price_tp;
   event.orderTicket  = (long)trans.order;
   event.ticket       = (long)trans.order;
   event.occurredAtMs = NowMs();

   AssignEventId(event);
   Enqueue(event);
}

//+------------------------------------------------------------------+
void AssignEventId(TBEvent &event)
{
   event.eventId = TB_BuildEventId(g_namespace, g_login, g_server, event.eventType,
                                   event.positionId, event.dealTicket,
                                   event.orderTicket, event.occurredAtMs,
                                   event.volume, event.price,
                                   event.stopLoss, event.takeProfit);
}

//+------------------------------------------------------------------+
void Enqueue(const TBEvent &event)
{
   int size = ArraySize(g_queue);
   if(size >= 10000)
   {
      // Bounded buffer. Dropping the OLDEST is correct: a 10,000-event backlog means
      // a long outage, and the newest events are the ones still worth acting on.
      for(int i = 1; i < size; i++) g_queue[i - 1] = g_queue[i];
      ArrayResize(g_queue, size - 1);
      size--;
   }
   ArrayResize(g_queue, size + 1);
   g_queue[size] = event;

   if(InpVerboseLog)
      Print("TradeBridge queued ", event.eventType, " ", event.symbol,
            " id=", StringSubstr(event.eventId, 0, 8));
}

//+------------------------------------------------------------------+
void FlushQueue()
{
   int total = ArraySize(g_queue);
   if(total == 0) return;

   int count = MathMin(total, InpBatchMax);
   string body = "{" + TB_JsonStr("server", g_server) + ",\"events\":[";
   for(int i = 0; i < count; i++)
   {
      if(i > 0) body += ",";
      body += EventToJson(g_queue[i]);
   }
   body += "]}";

   string response;
   if(g_client.Post("/api/v1/ea/master/events", body, response))
   {
      // ACCEPTED, DUPLICATE and IGNORED are all terminal: drop them from the queue.
      // Anything else stays and is retried.
      for(int i = count; i < total; i++) g_queue[i - count] = g_queue[i];
      ArrayResize(g_queue, total - count);

      g_failures = 0;
      g_nextRetryAt = 0;
      g_spool.Clear();
      if(InpVerboseLog) Print("TradeBridge delivered ", count, " event(s)");
   }
   else
   {
      g_failures++;
      int delay = TB_BackoffSeconds(g_failures);
      g_nextRetryAt = TimeCurrent() + delay;
      PersistQueue();

      if(g_client.LastStatus() == 401)
         Print("TradeBridge: authentication rejected. Events are spooled, not lost. "
               "Re-issue the install code from the dashboard.");
      else if(g_failures == 1 || g_failures % 10 == 0)
         Print("TradeBridge: delivery failed (", g_client.LastError(),
               "), retrying in ", delay, "s. Queued: ", total);
   }
}

//+------------------------------------------------------------------+
string EventToJson(const TBEvent &event)
{
   int digits = (int)SymbolInfoInteger(event.symbol, SYMBOL_DIGITS);
   if(digits <= 0) digits = 5;

   string json = "{";
   json += TB_JsonStr("event_id", event.eventId) + ",";
   json += TB_JsonStr("event_type", event.eventType) + ",";
   json += TB_JsonInt("ticket", event.ticket) + ",";
   json += TB_JsonInt("position_id", event.positionId) + ",";
   json += TB_JsonInt("order_ticket", event.orderTicket) + ",";
   json += TB_JsonInt("deal_ticket", event.dealTicket) + ",";
   json += TB_JsonStr("symbol", event.symbol) + ",";
   json += TB_JsonStr("side", event.side) + ",";
   json += TB_JsonNum("volume", event.volume, 2) + ",";
   json += TB_JsonNum("price", event.price, digits) + ",";
   json += (event.stopLoss   > 0 ? TB_JsonNum("stop_loss", event.stopLoss, digits)
                                 : TB_JsonNull("stop_loss")) + ",";
   json += (event.takeProfit > 0 ? TB_JsonNum("take_profit", event.takeProfit, digits)
                                 : TB_JsonNull("take_profit")) + ",";
   json += (event.prevStopLoss   > 0 ? TB_JsonNum("prev_stop_loss", event.prevStopLoss, digits)
                                     : TB_JsonNull("prev_stop_loss")) + ",";
   json += (event.prevTakeProfit > 0 ? TB_JsonNum("prev_take_profit", event.prevTakeProfit, digits)
                                     : TB_JsonNull("prev_take_profit")) + ",";
   json += TB_JsonNum("profit", event.profit, 2) + ",";
   json += TB_JsonNum("commission", event.commission, 2) + ",";
   json += TB_JsonNum("swap", event.swap, 2) + ",";
   json += TB_JsonInt("magic_number", event.magic) + ",";
   json += TB_JsonStr("comment", event.comment) + ",";
   json += TB_JsonStr("occurred_at", TB_IsoUtc(event.occurredAtMs));
   json += "}";
   return json;
}

//+------------------------------------------------------------------+
void SendHeartbeat()
{
   string body = "{";
   body += TB_JsonNum("balance", AccountInfoDouble(ACCOUNT_BALANCE), 2) + ",";
   body += TB_JsonNum("equity", AccountInfoDouble(ACCOUNT_EQUITY), 2) + ",";
   body += TB_JsonNum("margin", AccountInfoDouble(ACCOUNT_MARGIN), 2) + ",";
   body += TB_JsonNum("free_margin", AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ",";
   body += TB_JsonInt("open_positions", PositionsTotal()) + ",";
   body += TB_JsonStr("ea_version", "1.00") + ",";
   body += TB_JsonInt("terminal_build", TerminalInfoInteger(TERMINAL_BUILD));
   body += "}";

   string response;
   if(!g_client.Post("/api/v1/ea/master/heartbeat", body, response)) return;

   // Clock drift will start failing HMAC timestamp validation at 120s. Warn early.
   string serverTime = TB_JsonGet(response, "server_time");
   if(serverTime != "" && InpVerboseLog)
      Print("TradeBridge heartbeat ok, server time ", serverTime);
}

//+------------------------------------------------------------------+
bool Register()
{
   if(InpInstallCode == "")
   {
      Print("TradeBridge: InpInstallCode is empty. Generate one in the dashboard.");
      return false;
   }

   string body = "{";
   body += TB_JsonStr("install_code", InpInstallCode) + ",";
   body += TB_JsonStr("kind", "MASTER") + ",";
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
//| Credentials live in a terminal file, not in inputs, so a shared   |
//| chart template never carries them.                               |
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
   return "tradebridge_master_" + IntegerToString(g_login) + ".cred";
}

//+------------------------------------------------------------------+
void PersistQueue()
{
   g_spool.Clear();
   for(int i = 0; i < ArraySize(g_queue); i++)
      g_spool.Append(EventToJson(g_queue[i]));
}

//+------------------------------------------------------------------+
//| Replay the spool on startup. Deterministic event ids make this    |
//| safe: the server deduplicates anything already delivered.         |
//+------------------------------------------------------------------+
void RestoreSpool()
{
   string lines[];
   int count = g_spool.Drain(lines);
   if(count == 0) return;

   Print("TradeBridge: replaying ", count, " spooled event(s) from disk.");
   for(int i = 0; i < count; i++)
   {
      TBEvent event;
      ZeroEvent(event);
      event.eventId      = TB_JsonGet(lines[i], "event_id");
      event.eventType    = TB_JsonGet(lines[i], "event_type");
      event.symbol       = TB_JsonGet(lines[i], "symbol");
      event.side         = TB_JsonGet(lines[i], "side");
      event.comment      = TB_JsonGet(lines[i], "comment");
      event.volume       = TB_JsonGetDouble(lines[i], "volume");
      event.price        = TB_JsonGetDouble(lines[i], "price");
      event.stopLoss     = TB_JsonGetDouble(lines[i], "stop_loss");
      event.takeProfit   = TB_JsonGetDouble(lines[i], "take_profit");
      event.profit       = TB_JsonGetDouble(lines[i], "profit");
      event.commission   = TB_JsonGetDouble(lines[i], "commission");
      event.swap         = TB_JsonGetDouble(lines[i], "swap");
      event.ticket       = TB_JsonGetLong(lines[i], "ticket");
      event.positionId   = TB_JsonGetLong(lines[i], "position_id");
      event.orderTicket  = TB_JsonGetLong(lines[i], "order_ticket");
      event.dealTicket   = TB_JsonGetLong(lines[i], "deal_ticket");
      event.magic        = TB_JsonGetLong(lines[i], "magic_number");
      event.occurredAtMs = NowMs();
      if(event.eventId != "") Enqueue(event);
   }
}

//+------------------------------------------------------------------+
//| Position snapshots, so TRADE_MODIFIED can report prev SL/TP.      |
//+------------------------------------------------------------------+
void RebuildSnapshots()
{
   ArrayResize(g_snapshots, 0);
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      UpdateSnapshot((long)ticket);
   }
}

int FindSnapshot(const long positionId)
{
   for(int i = 0; i < ArraySize(g_snapshots); i++)
      if(g_snapshots[i].positionId == positionId) return i;
   return -1;
}

void UpdateSnapshot(const long positionId)
{
   if(!PositionSelectByTicket(positionId)) return;

   int index = FindSnapshot(positionId);
   if(index < 0)
   {
      index = ArraySize(g_snapshots);
      ArrayResize(g_snapshots, index + 1);
   }
   g_snapshots[index].positionId = positionId;
   g_snapshots[index].stopLoss   = PositionGetDouble(POSITION_SL);
   g_snapshots[index].takeProfit = PositionGetDouble(POSITION_TP);
   g_snapshots[index].volume     = PositionGetDouble(POSITION_VOLUME);
}

void RemoveSnapshot(const long positionId)
{
   int index = FindSnapshot(positionId);
   if(index < 0) return;
   int last = ArraySize(g_snapshots) - 1;
   g_snapshots[index] = g_snapshots[last];
   ArrayResize(g_snapshots, last);
}

//+------------------------------------------------------------------+
void ZeroEvent(TBEvent &event)
{
   event.eventId = ""; event.eventType = ""; event.symbol = "";
   event.side = ""; event.comment = "";
   event.volume = 0; event.price = 0; event.stopLoss = 0; event.takeProfit = 0;
   event.prevStopLoss = 0; event.prevTakeProfit = 0;
   event.profit = 0; event.commission = 0; event.swap = 0;
   event.ticket = 0; event.positionId = 0; event.orderTicket = 0;
   event.dealTicket = 0; event.magic = 0; event.occurredAtMs = 0;
}

long NowMs()
{
   return (long)TimeGMT() * 1000 + (GetTickCount() % 1000);
}

bool IsPendingOrderType(const ENUM_ORDER_TYPE type)
{
   return type == ORDER_TYPE_BUY_LIMIT  || type == ORDER_TYPE_SELL_LIMIT ||
          type == ORDER_TYPE_BUY_STOP   || type == ORDER_TYPE_SELL_STOP  ||
          type == ORDER_TYPE_BUY_STOP_LIMIT || type == ORDER_TYPE_SELL_STOP_LIMIT;
}

bool IsBuyOrderType(const ENUM_ORDER_TYPE type)
{
   return type == ORDER_TYPE_BUY || type == ORDER_TYPE_BUY_LIMIT ||
          type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_BUY_STOP_LIMIT;
}
