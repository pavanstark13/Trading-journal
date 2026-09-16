//+------------------------------------------------------------------+
//|  Client.mqh -- signed HTTPS transport with disk spooling          |
//|                                                                  |
//|  WebRequest() is SYNCHRONOUS: it blocks the calling thread until  |
//|  the server answers or the timeout elapses. Nothing in this file  |
//|  may be called from OnTradeTransaction(). Flush from OnTimer().   |
//+------------------------------------------------------------------+
#property strict

#include <Journal/Crypto.mqh>

#define J_ERR_URL_NOT_ALLOWED 4060

//+------------------------------------------------------------------+
class CJournalClient
{
private:
   string m_baseUrl;
   string m_apiKeyId;
   string m_apiSecret;
   int    m_timeoutMs;
   int    m_lastStatus;
   string m_lastError;

public:
   CJournalClient(): m_timeoutMs(10000), m_lastStatus(0) {}

   void Configure(const string baseUrl, const string apiKeyId, const string apiSecret)
   {
      m_baseUrl   = baseUrl;
      m_apiKeyId  = apiKeyId;
      m_apiSecret = apiSecret;
   }

   void SetTimeout(const int ms)        { m_timeoutMs = ms; }
   int    LastStatus() const            { return m_lastStatus; }
   string LastError()  const            { return m_lastError;  }
   bool   HasCredentials() const        { return m_apiKeyId != "" && m_apiSecret != ""; }

   //--- Signed request. Returns true on a 2xx response.
   bool Post(const string path, const string body, string &response)
   {
      return Send("POST", path, body, response, m_timeoutMs);
   }

   bool Get(const string path, string &response, const int timeoutMs = 0)
   {
      return Send("GET", path, "", response, timeoutMs > 0 ? timeoutMs : m_timeoutMs);
   }

   //--- Unsigned: registration only, where the install code IS the credential.
   bool PostUnsigned(const string path, const string body, string &response)
   {
      string headers = "Content-Type: application/json\r\n";
      return Execute("POST", m_baseUrl + path, headers, body, response, m_timeoutMs);
   }

private:
   bool Send(const string method, const string path, const string body,
             string &response, const int timeoutMs)
   {
      if(!HasCredentials())
      {
         m_lastError = "EA is not registered";
         return false;
      }

      string timestamp = IntegerToString((long)TimeGMT());
      string nonce     = J_Nonce();
      string signature = J_SignRequest(m_apiSecret, timestamp, nonce, body);

      string headers =
         "Content-Type: application/json\r\n" +
         "X-EA-Key: "       + m_apiKeyId  + "\r\n" +
         "X-EA-Timestamp: " + timestamp   + "\r\n" +
         "X-EA-Nonce: "     + nonce       + "\r\n" +
         "X-EA-Signature: " + signature   + "\r\n";

      return Execute(method, m_baseUrl + path, headers, body, response, timeoutMs);
   }

   bool Execute(const string method, const string url, const string headers,
                const string body, string &response, const int timeoutMs)
   {
      char   post[];
      char   result[];
      string responseHeaders;

      if(StringLen(body) > 0)
      {
         int len = StringToCharArray(body, post, 0, WHOLE_ARRAY, CP_UTF8);
         if(len > 0) ArrayResize(post, len - 1);   // drop the null terminator
      }
      else
      {
         ArrayResize(post, 0);
      }

      ResetLastError();
      m_lastStatus = WebRequest(method, url, headers, timeoutMs, post, result, responseHeaders);

      if(m_lastStatus == -1)
      {
         int code = GetLastError();
         if(code == J_ERR_URL_NOT_ALLOWED)
         {
            m_lastError = "URL not whitelisted";
            Print("Journal FATAL: add ", m_baseUrl,
                  " under Tools > Options > Expert Advisors > "
                  "'Allow WebRequest for listed URL', then restart the EA.");
         }
         else
         {
            m_lastError = "WebRequest failed, error " + IntegerToString(code);
         }
         response = "";
         return false;
      }

      response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);

      if(m_lastStatus >= 200 && m_lastStatus < 300)
      {
         m_lastError = "";
         return true;
      }

      m_lastError = "HTTP " + IntegerToString(m_lastStatus);
      return false;
   }
};

//+------------------------------------------------------------------+
//| Append-only disk spool: survives terminal restarts and network    |
//| outages. Bounded so a long outage cannot fill the disk.           |
//+------------------------------------------------------------------+
class CJournalSpool
{
private:
   string m_filename;
   int    m_maxLines;

public:
   CJournalSpool(): m_maxLines(10000) {}

   void Configure(const string filename, const int maxLines = 10000)
   {
      m_filename = filename;
      m_maxLines = maxLines;
   }

   bool Append(const string line)
   {
      int handle = FileOpen(m_filename, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE) return false;
      FileSeek(handle, 0, SEEK_END);
      FileWriteString(handle, line + "\n");
      FileClose(handle);
      return true;
   }

   //--- Read every spooled line, then clear the file.
   int Drain(string &lines[])
   {
      ArrayResize(lines, 0);
      if(!FileIsExist(m_filename)) return 0;

      int handle = FileOpen(m_filename, FILE_READ | FILE_TXT | FILE_ANSI);
      if(handle == INVALID_HANDLE) return 0;

      int count = 0;
      while(!FileIsEnding(handle) && count < m_maxLines)
      {
         string line = FileReadString(handle);
         if(StringLen(line) == 0) continue;
         ArrayResize(lines, count + 1);
         lines[count++] = line;
      }
      FileClose(handle);
      FileDelete(m_filename);
      return count;
   }

   void Clear()
   {
      if(FileIsExist(m_filename)) FileDelete(m_filename);
   }
};

//+------------------------------------------------------------------+
//| Exponential backoff: 1, 2, 4, 8, 16, capped at 30 seconds.        |
//+------------------------------------------------------------------+
int J_BackoffSeconds(const int consecutiveFailures)
{
   if(consecutiveFailures <= 0) return 0;
   int delay = (int)MathPow(2, MathMin(consecutiveFailures - 1, 5));
   return MathMin(delay, 30);
}
