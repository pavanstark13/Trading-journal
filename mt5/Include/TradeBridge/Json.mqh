//+------------------------------------------------------------------+
//|  Json.mqh -- minimal JSON writing and flat-value extraction       |
//|                                                                  |
//|  Deliberately small. The EA builds well-known payloads and reads  |
//|  a handful of fields back; a general parser would be more code to |
//|  debug inside a terminal, where debugging is painful.             |
//+------------------------------------------------------------------+
#property strict

//+------------------------------------------------------------------+
string TB_JsonEscape(const string value)
{
   string out = value;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\n", "\\n");
   StringReplace(out, "\r", "\\r");
   StringReplace(out, "\t", "\\t");
   return out;
}

string TB_JsonStr(const string key, const string value)
{
   return "\"" + key + "\":\"" + TB_JsonEscape(value) + "\"";
}

string TB_JsonNum(const string key, const double value, const int digits)
{
   return "\"" + key + "\":" + DoubleToString(value, digits);
}

string TB_JsonInt(const string key, const long value)
{
   return "\"" + key + "\":" + IntegerToString(value);
}

string TB_JsonBool(const string key, const bool value)
{
   return "\"" + key + "\":" + (value ? "true" : "false");
}

string TB_JsonNull(const string key)
{
   return "\"" + key + "\":null";
}

//+------------------------------------------------------------------+
//| ISO-8601 UTC with milliseconds, which is what the API expects.    |
//+------------------------------------------------------------------+
string TB_IsoUtc(const long epochMs)
{
   datetime seconds = (datetime)(epochMs / 1000);
   int      millis  = (int)(epochMs % 1000);
   MqlDateTime dt;
   TimeToStruct(seconds, dt);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                       dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec, millis);
}

//+------------------------------------------------------------------+
//| Extract a flat value for "key" from a JSON object. Returns "" if  |
//| absent. Handles strings, numbers, booleans and null.              |
//+------------------------------------------------------------------+
string TB_JsonGet(const string json, const string key)
{
   string needle = "\"" + key + "\"";
   int at = StringFind(json, needle);
   if(at < 0) return "";

   int colon = StringFind(json, ":", at + StringLen(needle));
   if(colon < 0) return "";

   int i = colon + 1;
   int len = StringLen(json);
   while(i < len && (StringGetCharacter(json, i) == ' ' ||
                     StringGetCharacter(json, i) == '\n' ||
                     StringGetCharacter(json, i) == '\r' ||
                     StringGetCharacter(json, i) == '\t')) i++;
   if(i >= len) return "";

   if(StringGetCharacter(json, i) == '"')
   {
      int start = i + 1;
      int j = start;
      while(j < len)
      {
         ushort ch = StringGetCharacter(json, j);
         if(ch == '\\') { j += 2; continue; }
         if(ch == '"')  break;
         j++;
      }
      return StringSubstr(json, start, j - start);
   }

   int start = i;
   while(i < len)
   {
      ushort ch = StringGetCharacter(json, i);
      if(ch == ',' || ch == '}' || ch == ']') break;
      i++;
   }
   string raw = StringSubstr(json, start, i - start);
   StringTrimLeft(raw);
   StringTrimRight(raw);
   return raw;
}

bool TB_JsonGetBool(const string json, const string key, const bool fallback = false)
{
   string value = TB_JsonGet(json, key);
   if(value == "") return fallback;
   return value == "true";
}

double TB_JsonGetDouble(const string json, const string key, const double fallback = 0.0)
{
   string value = TB_JsonGet(json, key);
   if(value == "" || value == "null") return fallback;
   return StringToDouble(value);
}

long TB_JsonGetLong(const string json, const string key, const long fallback = 0)
{
   string value = TB_JsonGet(json, key);
   if(value == "" || value == "null") return fallback;
   return StringToInteger(value);
}

//+------------------------------------------------------------------+
//| Split the objects of a top-level JSON array field into elements.  |
//| Brace-depth aware, so nested objects stay intact.                 |
//+------------------------------------------------------------------+
int TB_JsonObjects(const string json, const string arrayKey, string &out[])
{
   ArrayResize(out, 0);
   string needle = "\"" + arrayKey + "\"";
   int at = StringFind(json, needle);
   if(at < 0) return 0;

   int open = StringFind(json, "[", at);
   if(open < 0) return 0;

   int depth = 0, count = 0, start = -1;
   int len = StringLen(json);
   for(int i = open; i < len; i++)
   {
      ushort ch = StringGetCharacter(json, i);
      if(ch == '{')
      {
         if(depth == 0) start = i;
         depth++;
      }
      else if(ch == '}')
      {
         depth--;
         if(depth == 0 && start >= 0)
         {
            ArrayResize(out, count + 1);
            out[count++] = StringSubstr(json, start, i - start + 1);
            start = -1;
         }
      }
      else if(ch == ']' && depth == 0)
      {
         break;
      }
   }
   return count;
}
