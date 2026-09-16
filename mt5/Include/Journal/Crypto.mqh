//+------------------------------------------------------------------+
//|  Crypto.mqh -- SHA-256 and HMAC-SHA256 request signing            |
//|                                                                  |
//|  MQL5's CryptEncode(CRYPT_HASH_SHA256, ...) ignores the key       |
//|  argument, so HMAC is constructed by hand from the RFC 2104       |
//|  ipad/opad definition. The output must match Python's             |
//|  hmac.new(secret, msg, sha256).hexdigest() exactly.               |
//+------------------------------------------------------------------+
#property strict

//+------------------------------------------------------------------+
void J_Sha256(const uchar &data[], uchar &out[])
{
   uchar key[];
   ArrayResize(key, 0);
   CryptEncode(CRYPT_HASH_SHA256, data, key, out);
}

//+------------------------------------------------------------------+
//| Convert a string to its UTF-8 bytes WITHOUT a trailing null.      |
//| StringToCharArray appends a terminator; signing it would break    |
//| parity with the server, so it is always trimmed here.             |
//+------------------------------------------------------------------+
void J_StringToBytes(const string text, uchar &out[])
{
   int len = StringToCharArray(text, out, 0, WHOLE_ARRAY, CP_UTF8);
   if(len > 0 && out[len - 1] == 0)
      ArrayResize(out, len - 1);
   else
      ArrayResize(out, len);
}

//+------------------------------------------------------------------+
string J_BytesToHex(const uchar &data[])
{
   string hex = "";
   for(int i = 0; i < ArraySize(data); i++)
      hex += StringFormat("%02x", data[i]);
   return hex;
}

//+------------------------------------------------------------------+
//| HMAC-SHA256. Mirrors app/core/crypto.py:compute_ea_signature.     |
//+------------------------------------------------------------------+
void J_HmacSha256(const uchar &key[], const uchar &message[], uchar &out[])
{
   const int BLOCK = 64;
   uchar padded[];
   ArrayResize(padded, BLOCK);
   ArrayInitialize(padded, 0);

   if(ArraySize(key) > BLOCK)
   {
      uchar hashed[];
      J_Sha256(key, hashed);
      ArrayCopy(padded, hashed, 0, 0, ArraySize(hashed));
   }
   else
   {
      ArrayCopy(padded, key, 0, 0, ArraySize(key));
   }

   uchar inner[], outer[];
   ArrayResize(inner, BLOCK + ArraySize(message));
   ArrayResize(outer, BLOCK + 32);

   for(int i = 0; i < BLOCK; i++)
   {
      inner[i] = (uchar)(padded[i] ^ 0x36);
      outer[i] = (uchar)(padded[i] ^ 0x5C);
   }
   ArrayCopy(inner, message, BLOCK, 0, ArraySize(message));

   uchar innerHash[];
   J_Sha256(inner, innerHash);
   ArrayCopy(outer, innerHash, BLOCK, 0, 32);

   J_Sha256(outer, out);
}

//+------------------------------------------------------------------+
string J_SignRequest(const string secret, const string timestamp,
                      const string nonce, const string body)
{
   uchar keyBytes[], messageBytes[], signature[];
   J_StringToBytes(secret, keyBytes);
   J_StringToBytes(timestamp + "." + nonce + "." + body, messageBytes);
   J_HmacSha256(keyBytes, messageBytes, signature);
   return J_BytesToHex(signature);
}

//+------------------------------------------------------------------+
//| 32 hex characters of request nonce. Uniqueness matters more than  |
//| cryptographic quality: the server also enforces a timestamp       |
//| window, so a nonce only needs to be unique within 5 minutes.      |
//+------------------------------------------------------------------+
string J_Nonce()
{
   string raw = "";
   for(int i = 0; i < 8; i++)
      raw += StringFormat("%04x", MathRand());
   return StringSubstr(raw, 0, 32);
}
