//+------------------------------------------------------------------+
//|  EventId.mqh -- deterministic event identity                      |
//|                                                                  |
//|  MUST stay byte-identical to app/domain/events.py:build_event_id. |
//|  Determinism is what makes at-least-once delivery safe: an EA     |
//|  replaying its disk spool after a crash regenerates the SAME id,  |
//|  so the server deduplicates instead of publishing twice.          |
//|                                                                  |
//|  Verified in CI against mt5/tests/event_id_vectors.json.          |
//+------------------------------------------------------------------+
#property strict

#include <TradeBridge/Crypto.mqh>

//+------------------------------------------------------------------+
//| Fixed-width numeric formatting. Must match the Python _fmt().     |
//+------------------------------------------------------------------+
string TB_Num(const double value, const int places)
{
   return DoubleToString(value, places);
}

//+------------------------------------------------------------------+
//| First 16 hex chars of sha256 over the mutable numeric state.      |
//+------------------------------------------------------------------+
string TB_StateHash(const double volume, const double price,
                    const double stopLoss, const double takeProfit)
{
   string raw = TB_Num(volume, 2) + "|" + TB_Num(price, 5) + "|" +
                TB_Num(stopLoss, 5) + "|" + TB_Num(takeProfit, 5);
   uchar bytes[], digest[];
   TB_StringToBytes(raw, bytes);
   TB_Sha256(bytes, digest);
   return StringSubstr(TB_BytesToHex(digest), 0, 16);
}

//+------------------------------------------------------------------+
//| Event types whose identity depends on mutable state rather than   |
//| on a unique deal ticket.                                          |
//+------------------------------------------------------------------+
bool TB_IsStatefulType(const string eventType)
{
   return eventType == "TRADE_MODIFIED" || eventType == "PENDING_ORDER_MODIFIED";
}

//+------------------------------------------------------------------+
string TB_BuildEventId(const uchar &namespaceBytes[],
                       const long   mt5Login,
                       const string brokerServer,
                       const string eventType,
                       const long   positionId,
                       const long   dealTicket,
                       const long   orderTicket,
                       const long   occurredAtMs,
                       const double volume,
                       const double price,
                       const double stopLoss,
                       const double takeProfit)
{
   string tail = TB_IsStatefulType(eventType)
                 ? TB_StateHash(volume, price, stopLoss, takeProfit)
                 : "-";

   string name = IntegerToString(mt5Login) + "|" +
                 brokerServer + "|" +
                 eventType + "|" +
                 IntegerToString(positionId) + "|" +
                 IntegerToString(dealTicket) + "|" +
                 IntegerToString(orderTicket) + "|" +
                 IntegerToString(occurredAtMs) + "|" +
                 tail;

   uchar result[];
   TB_Uuid5(namespaceBytes, name, result);
   return TB_FormatUuid(result);
}
