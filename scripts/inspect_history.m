// Read-only inspection of a COPIED device Realm with the locked Realm library.
// Run inside a dedicated iOS simulator; never point this tool at a live database.
#import <Foundation/Foundation.h>
#import <CommonCrypto/CommonDigest.h>
#import <Realm/Realm.h>
#import "RLMRealm_Dynamic.h"
#import "RLMRealmConfiguration_Private.h"

static NSString *digest(NSData *data) {
    unsigned char bytes[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(data.bytes, (CC_LONG)data.length, bytes);
    NSMutableString *result = [NSMutableString string];
    for (NSUInteger i = 0; i < sizeof(bytes); i++) [result appendFormat:@"%02x", bytes[i]];
    return result;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc != 3 || !strlen(argv[2])) {
            fprintf(stderr, "usage: inspect_history COPIED.realm test-id-prefix\n");
            return 2;
        }
        @try {
            RLMRealmConfiguration *config = [RLMRealmConfiguration new];
            config.fileURL = [NSURL fileURLWithPath:@(argv[1])];
            config.readOnly = YES;
            config.dynamic = YES; // Use the file's actual schema, without app models.
            config.schemaVersion = 19;
            NSError *error;
            RLMRealm *realm = [RLMRealm realmWithConfiguration:config error:&error];
            if (!realm) { fprintf(stderr, "Could not open copied Realm\n"); return 1; }
            RLMResults<RLMObject *> *messages = [realm allObjects:@"Message"];
            NSMutableDictionary *rows = [NSMutableDictionary dictionary];
            NSMutableArray *tests = [NSMutableArray array];
            NSString *prefix = @(argv[2]);
            for (RLMObject *message in messages) {
                NSMutableDictionary *fields = [NSMutableDictionary dictionary];
                for (NSString *field in @[@"id", @"title", @"subtitle", @"body", @"bodyType", @"url", @"image", @"group", @"createDate", @"expireDate"]) {
                    id value = message[field];
                    if ([value isKindOfClass:NSDate.class]) value = @([value timeIntervalSince1970]);
                    fields[field] = value ?: NSNull.null;
                }
                NSData *data = [NSJSONSerialization dataWithJSONObject:fields options:NSJSONWritingSortedKeys error:&error];
                if (!data) return 1;
                NSString *identifier = fields[@"id"];
                rows[digest([identifier dataUsingEncoding:NSUTF8StringEncoding])] = @{
                    @"digest": digest(data), @"expires": fields[@"expireDate"]
                };
                // Only deliberately generated test records may expose their text.
                if ([identifier hasPrefix:prefix]) [tests addObject:fields];
            }
            NSDictionary *report = @{@"count": @(messages.count), @"schema": @(realm.configuration.schemaVersion),
                                     @"rows": rows, @"test_records": tests, @"read_only": @YES};
            NSData *json = [NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingSortedKeys error:&error];
            if (!json) return 1;
            fwrite(json.bytes, 1, json.length, stdout);
            fputs("\n", stdout);
        } @catch (NSException *exception) {
            fprintf(stderr, "Copied Realm inspection failed\n");
            return 1;
        }
    }
    return 0;
}
