#import <Foundation/Foundation.h>
#import "A.pbobjc.h"
#import "B.pbobjc.h"
#import "Main.pbobjc.h"
#include <assert.h>

int main(void) {
  @autoreleasepool {
    QAAItem *a = [QAAItem message];
    a.id_p = 0x01020304;
    const unsigned char fixed[] = {13, 4, 3, 2, 1};
    assert([a.data isEqualToData:[NSData dataWithBytes:fixed length:sizeof(fixed)]]);
    QABItem *b = [QABItem message];
    b.text = @"x";
    QAMMain *message = [QAMMain message];
    message.left = a;
    message.right = b;
    message.mode = QAAMode_On;
    message.text = @"discard";
    message.number = 7;
    assert(message.selectionOneOfCase == QAMMain_Selection_OneOfCase_Number);
    assert(message.text.length == 0);
    [message.valuesArray addValue:3];
    [message.valuesArray addValue:4];
    const unsigned char wire[] = {10, 5, 13, 4, 3, 2, 1, 18, 3, 10, 1, 120, 24, 1, 40, 7, 48, 3, 48, 4};
    NSData *expected = [NSData dataWithBytes:wire length:sizeof(wire)];
    assert([message.data isEqualToData:expected]);
    NSError *error = nil;
    QAMMain *decoded = [QAMMain parseFromData:expected error:&error];
    assert(decoded && !error && [decoded isEqual:message]);
    assert(decoded.left.id_p == 0x01020304 && [decoded.right.text isEqualToString:@"x"]);
    assert(decoded.valuesArray_Count == 2 && [decoded.valuesArray valueAtIndex:1] == 4);
    QAMMain *empty = [QAMMain message];
    assert(!empty.isInitialized && empty.data == nil);
    error = nil;
    assert([QAMMain parseFromData:[NSData data] error:&error] == nil && error);
    message.left = [QAAItem message];
    assert(!message.isInitialized && message.data == nil);
    puts("PASS Objective-C: cross-file classes, enum, oneof, repeated, exact bytes, required checks");
  }
  return 0;
}
