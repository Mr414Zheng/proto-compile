import com.google.protobuf.UninitializedMessageException;
import device.protocol.realtime_data.RealtimeData;
import device.protocol.weather.WeatherOuterClass;
import java.util.Arrays;

public final class JavaSmoke {
    public static void main(String[] args) throws Exception {
        var time = RealtimeData.RtTime.newBuilder().setSeconds(0x01020304).build();
        if (!Arrays.equals(time.toByteArray(), new byte[] {13, 4, 3, 2, 1})) {
            throw new AssertionError("fixed32 wire encoding changed");
        }
        var weather = WeatherOuterClass.Weather.newBuilder()
            .setDateTime(time).setType(WeatherOuterClass.WeatherType.EACH_HOUR)
            .setDesc(WeatherOuterClass.WeatherDesc.Sunny)
            .setDegreeMax(30).setDegreeMin(-5).setPm2P5(12).build();
        var decoded = WeatherOuterClass.Weather.parseFrom(weather.toByteArray());
        if (!decoded.equals(weather) || decoded.getDateTime().getSeconds() != 0x01020304) {
            throw new AssertionError("cross-file message roundtrip failed");
        }
        var confirm = WeatherOuterClass.WeatherConfirm.newBuilder()
            .setOperation(WeatherOuterClass.WeatherOperation.CLEAR).setRet(true).build();
        var subscriber = WeatherOuterClass.WeatherSubscriber.newBuilder().setHash(0x01020304)
            .setParams(WeatherOuterClass.WeatherParams.newBuilder().setMaxCount(10))
            .setConfirm(confirm).build();
        if (subscriber.hasParams() || !subscriber.hasConfirm()) {
            throw new AssertionError("oneof did not clear previous value");
        }
        var expected = new byte[] {13, 4, 3, 2, 1, 26, 4, 8, 1, 16, 1};
        if (!Arrays.equals(subscriber.toByteArray(), expected)
                || !WeatherOuterClass.WeatherSubscriber.parseFrom(expected).equals(subscriber)) {
            throw new AssertionError("enum/oneof wire encoding changed");
        }
        try {
            WeatherOuterClass.Weather.newBuilder().build();
            throw new AssertionError("proto2 required fields were lost");
        } catch (UninitializedMessageException expectedFailure) {
            // required 约束必须在生成代码中保留。
        }
        System.out.println("PASS: cross-file, enum, oneof, fixed32 wire bytes, required fields");
    }
}
