//+------------------------------------------------------------------+
//|  EventIdParity.mq5 -- run once in MetaEditor after any change to  |
//|  EventId.mqh or Crypto.mqh.                                       |
//|                                                                  |
//|  Deterministic event ids are the keystone of idempotency. If the  |
//|  MQL5 and Python implementations drift, duplicate trades get      |
//|  published and copied and nothing warns you. This script is the   |
//|  only place that check can happen, because WebRequest and the     |
//|  crypto functions are not available outside a terminal.           |
//|                                                                  |
//|  Expected values come from mt5/tests/event_id_vectors.json, which |
//|  the backend test suite regenerates.                              |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

#include <TradeBridge/Crypto.mqh>
#include <TradeBridge/EventId.mqh>

input string InpNamespace = "tradebridge.example.com";

int g_passed = 0;
int g_failed = 0;

//+------------------------------------------------------------------+
void Check(const string label, const string actual, const string expected)
{
   if(actual == expected)
   {
      g_passed++;
      Print("PASS  ", label);
   }
   else
   {
      g_failed++;
      Print("FAIL  ", label);
      Print("      expected: ", expected);
      Print("      actual:   ", actual);
   }
}

//+------------------------------------------------------------------+
void OnStart()
{
   uchar ns[16];
   TB_NamespaceUuid(InpNamespace, ns);

   // --- state hash vectors -------------------------------------------------
   Check("state_hash 0.50/1.17250/1.17250/1.17750",
         TB_StateHash(0.50, 1.17250, 1.17250, 1.17750),
         "a15a8bf886e79f0a");

   Check("state_hash zeros",
         TB_StateHash(0, 0, 0, 0),
         "7eaf74a0b819d703");

   // --- event id vectors ---------------------------------------------------
   Check("position opened",
         TB_BuildEventId(ns, 5012345, "ICMarketsSC-Live", "TRADE_OPENED",
                         123456, 987654, 123456, 1757998234123, 0, 0, 0, 0),
         "79ada9a2-ed96-5650-9a19-d1270882af78");

   Check("position closed",
         TB_BuildEventId(ns, 5012345, "ICMarketsSC-Live", "TRADE_CLOSED",
                         123456, 987654, 123456, 1757998234123, 0, 0, 0, 0),
         "27fa2e2a-5448-5c04-8c0e-e785497eea65");

   Check("partial close",
         TB_BuildEventId(ns, 5012345, "ICMarketsSC-Live", "TRADE_PARTIAL_CLOSED",
                         123456, 987654, 123456, 1757998234123, 0, 0, 0, 0),
         "99222f62-f8c5-5bc8-9904-8c723ef3e520");

   Check("stop moved to breakeven",
         TB_BuildEventId(ns, 5012345, "ICMarketsSC-Live", "TRADE_MODIFIED",
                         123456, 987654, 123456, 1757998234123, 0.50, 1.17250, 1.17250, 1.17750),
         "f9182546-1368-5432-aead-5f7c9cb267b0");

   Check("stop trailed further",
         TB_BuildEventId(ns, 5012345, "ICMarketsSC-Live", "TRADE_MODIFIED",
                         123456, 987654, 123456, 1757998234123, 0.50, 1.17250, 1.17400, 1.17750),
         "1002e0ee-31e3-5a2d-96ed-fbbbfd2847d1");

   Check("pending order created",
         TB_BuildEventId(ns, 5012345, "ICMarketsSC-Live", "PENDING_ORDER_CREATED",
                         123456, 0, 123456, 1757998234123, 0, 0, 0, 0),
         "6d28cf72-75a7-5996-9d77-58f098f1616d");

   Check("different account",
         TB_BuildEventId(ns, 9988776, "ICMarketsSC-Live", "TRADE_OPENED",
                         123456, 987654, 123456, 1757998234123, 0, 0, 0, 0),
         "c3c57508-1ccf-59c1-89dd-c440fd1b00a2");

   Print("----------------------------------------");
   Print("EventIdParity: ", g_passed, " passed, ", g_failed, " failed");
   if(g_failed > 0)
      Print("DO NOT DEPLOY. Fix EventId.mqh until every vector matches "
            "mt5/tests/event_id_vectors.json.");
}
